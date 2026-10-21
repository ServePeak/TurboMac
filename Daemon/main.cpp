#include "Calibration.h"
#include "DriverClient.h"
#include "Energy.h"
#include "Policy.h"
#include "Profile.h"
#include "SMCReader.h"
#include "TemperatureReader.h"

#include "../Shared/HWPCodec.h"
#include "../Shared/RAPLCodec.h"

#include <IOKit/ps/IOPSKeys.h>
#include <IOKit/ps/IOPowerSources.h>
#include <CoreFoundation/CoreFoundation.h>

#include <arpa/inet.h>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <poll.h>
#include <spawn.h>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

extern char **environ;

namespace {

constexpr const char *kSocketPath = "/var/run/turbomacd.sock";
constexpr const char *kLogPath = "/var/log/turbomacd.jsonl";
constexpr const char *kAVX2LoadPath = "/usr/local/libexec/turbomac-avx2-load";
constexpr const char *kWhisperValidationPath =
    "/Library/Application Support/TurboMac/whisper-validation";
constexpr double kRAPLSampleInterval = 0.1;
constexpr double kRAPLFallbackInterval = 1.0;
constexpr double kSMCFastInterval = 0.25;
constexpr double kSMCFallbackInterval = 1.0;
constexpr double kBatterySampleInterval = 1.0;
constexpr uint32_t kTelemetryRecoverySamples = 20U;
constexpr double kAutoArmTelemetrySeconds = 10.0;
constexpr double kHardCalibrationInputW = 52.0;
constexpr double kSoftCalibrationInputW = 50.0;
constexpr double kMaximumCPUTemperatureC = 95.0;
constexpr double kCalibrationStatisticsWarmupSeconds = 2.0;

volatile sig_atomic_t stopRequested = 0;

void handleSignal(int) {
    stopRequested = 1;
}

double monotonicSeconds() {
    timespec now = {};
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)now.tv_sec + (double)now.tv_nsec / 1e9;
}

std::string utcTimestamp() {
    timespec now = {};
    clock_gettime(CLOCK_REALTIME, &now);
    tm utc = {};
    gmtime_r(&now.tv_sec, &utc);
    char buffer[64];
    std::snprintf(
        buffer,
        sizeof(buffer),
        "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ",
        utc.tm_year + 1900,
        utc.tm_mon + 1,
        utc.tm_mday,
        utc.tm_hour,
        utc.tm_min,
        utc.tm_sec,
        now.tv_nsec / 1000000L
    );
    return buffer;
}

// State of charge of the internal battery in percent, or -1.0 when no
// plausible reading exists. Fail-safe sources: IOPowerSources is the
// documented macOS interface, and any absent/garbled dictionary leaves
// the buffered allowance disabled in the policy.
double readBatteryPercent() {
    CFTypeRef info = IOPSCopyPowerSourcesInfo();
    if (info == nullptr) {
        return -1.0;
    }
    double result = -1.0;
    CFArrayRef sources = IOPSCopyPowerSourcesList(info);
    if (sources != nullptr) {
        const CFIndex count = CFArrayGetCount(sources);
        for (CFIndex index = 0; index < count; ++index) {
            const CFTypeRef source = CFArrayGetValueAtIndex(sources, index);
            const CFDictionaryRef description =
                IOPSGetPowerSourceDescription(info, source);
            if (description == nullptr) {
                continue;
            }
            const CFTypeRef type =
                CFDictionaryGetValue(description, CFSTR(kIOPSTypeKey));
            if (type == nullptr
                || CFGetTypeID(type) != CFStringGetTypeID()
                || CFStringCompare(
                    (CFStringRef)type,
                    CFSTR(kIOPSInternalBatteryType),
                    0
                ) != kCFCompareEqualTo) {
                continue;
            }
            const CFTypeRef capacity = CFDictionaryGetValue(
                description,
                CFSTR(kIOPSCurrentCapacityKey)
            );
            double percent = -1.0;
            if (capacity != nullptr
                && CFGetTypeID(capacity) == CFNumberGetTypeID()
                && CFNumberGetValue(
                    (CFNumberRef)capacity,
                    kCFNumberDoubleType,
                    &percent
                )
                && percent >= 0.0
                && percent <= 100.0) {
                result = percent;
            }
            break;
        }
        CFRelease(sources);
    }
    CFRelease(info);
    return result;
}

std::string jsonEscape(const std::string &input) {
    std::ostringstream output;
    for (unsigned char character : input) {
        switch (character) {
            case '"': output << "\\\""; break;
            case '\\': output << "\\\\"; break;
            case '\b': output << "\\b"; break;
            case '\f': output << "\\f"; break;
            case '\n': output << "\\n"; break;
            case '\r': output << "\\r"; break;
            case '\t': output << "\\t"; break;
            default:
                if (character < 0x20U) {
                    output << "\\u" << std::hex << std::setw(4)
                           << std::setfill('0') << (unsigned)character;
                } else {
                    output << character;
                }
        }
    }
    return output.str();
}

bool writeAll(int descriptor, const std::string &content) {
    size_t written = 0U;
    while (written < content.size()) {
        const ssize_t count = ::write(
            descriptor, content.data() + written, content.size() - written
        );
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return false;
        }
        written += (size_t)count;
    }
    return true;
}

void sleepMilliseconds(unsigned milliseconds) {
    timespec delay = {
        .tv_sec = (time_t)(milliseconds / 1000U),
        .tv_nsec = (long)(milliseconds % 1000U) * 1000000L,
    };
    while (nanosleep(&delay, &delay) != 0 && errno == EINTR && !stopRequested) {}
}

uint32_t wattsToMW(double watts) {
    if (!std::isfinite(watts) || watts <= 0.0) {
        return 0U;
    }
    return (uint32_t)std::llround(watts * 1000.0);
}

double rawLimitW(uint64_t raw, bool second, uint32_t exponent) {
    const uint32_t field = second ? tm_rapl_pl2_raw(raw) : tm_rapl_pl1_raw(raw);
    return (double)tm_rapl_raw_to_mw(field, exponent) / 1000.0;
}

bool protectedExecutable(const char *path, std::string *error) {
    struct stat status = {};
    if (lstat(path, &status) != 0
        || !S_ISREG(status.st_mode)
        || status.st_uid != 0U
        || (status.st_mode & 0022U) != 0U
        || (status.st_mode & 0111U) == 0U) {
        if (error != nullptr) {
            *error = std::string(path)
                + " must be a root-owned, non-writable executable regular file";
        }
        return false;
    }
    return true;
}

bool spawnProgram(
    const char *path,
    const std::vector<std::string> &arguments,
    pid_t *process,
    std::string *error
) {
    std::vector<char *> argv;
    argv.reserve(arguments.size() + 2U);
    argv.push_back(const_cast<char *>(path));
    for (const std::string &argument : arguments) {
        argv.push_back(const_cast<char *>(argument.c_str()));
    }
    argv.push_back(nullptr);
    const int result = posix_spawn(process, path, nullptr, nullptr, argv.data(), environ);
    if (result != 0) {
        if (error != nullptr) {
            *error = std::string("posix_spawn failed: ") + std::strerror(result);
        }
        return false;
    }
    return true;
}

void terminateChild(pid_t process) {
    if (process <= 0) {
        return;
    }
    kill(process, SIGTERM);
    for (unsigned attempt = 0U; attempt < 20U; ++attempt) {
        int status = 0;
        if (waitpid(process, &status, WNOHANG) == process) {
            return;
        }
        sleepMilliseconds(50U);
    }
    kill(process, SIGKILL);
    while (waitpid(process, nullptr, 0) < 0 && errno == EINTR) {}
}

class EventLog {
public:
    EventLog() : descriptor_(-1) {}
    ~EventLog() {
        if (descriptor_ >= 0) {
            close(descriptor_);
        }
    }

    bool openLog(std::string *error) {
        rotate();
        descriptor_ = ::open(kLogPath, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
        if (descriptor_ < 0) {
            if (error != nullptr) {
                *error = "could not open daemon log";
            }
            return false;
        }
        fchown(descriptor_, 0, 0);
        fchmod(descriptor_, 0600);
        return true;
    }

    void write(const std::string &event, const std::string &detail) {
        if (descriptor_ < 0) {
            return;
        }
        const std::string entry = "{\"timestamp\":\"" + utcTimestamp()
            + "\",\"event\":\"" + jsonEscape(event)
            + "\",\"detail\":\"" + jsonEscape(detail) + "\"}\n";
        writeAll(descriptor_, entry);
    }

private:
    void rotate() {
        struct stat status = {};
        if (stat(kLogPath, &status) != 0 || status.st_size < 10 * 1024 * 1024) {
            return;
        }
        const std::string first = std::string(kLogPath) + ".1";
        const std::string second = std::string(kLogPath) + ".2";
        unlink(second.c_str());
        rename(first.c_str(), second.c_str());
        rename(kLogPath, first.c_str());
    }

    int descriptor_;
};

struct LoadStatistics {
    double inputTotal = 0.0;
    double packageTotal = 0.0;
    double peakInput = 0.0;
    double peakTemperature = 0.0;
    double worstTransientError = 0.0;
    size_t samples = 0U;

    double averageInput() const {
        return samples == 0U ? 0.0 : inputTotal / (double)samples;
    }
    double averagePackage() const {
        return samples == 0U ? 0.0 : packageTotal / (double)samples;
    }
};

enum class LoadResult {
    Complete,
    SoftCeiling,
    Abort,
};

class TurboMacDaemon {
public:
    TurboMacDaemon()
        : smcOpen_(false), policy_(PolicyConfig{}), profileValid_(false),
          armed_(false), operationActive_(false), listenFD_(-1),
          nextRAPLTime_(0.0), nextSMCTime_(0.0), nextBatteryTime_(0.0),
          batteryPercent_(-1.0), lastTelemetryLogTime_(0.0),
          lastDriverUpdateTime_(0.0), validTelemetrySince_(-1.0),
          latestTemperatureC_(NAN), latestCPUDieTemperatureC_(NAN),
          lastCPUDieReadTime_(-1.0), latestCapacityW_(0.0), latestInputW_(0.0),
          smcFailures_(0U), smcSuccesses_(0U), smcErrorSerial_(0U),
          raplSuccesses_(0U), raplErrorSerial_(0U), smcSampleSerial_(0U),
          lastLoggedBand_(GovernorBand::Cruise) {
        std::memset(&smc_, 0, sizeof(smc_));
        std::memset(&capabilities_, 0, sizeof(capabilities_));
        std::memset(&telemetry_, 0, sizeof(telemetry_));
    }

    ~TurboMacDaemon() {
        std::string ignored;
        if (armed_) {
            driver_.disarm(&ignored);
        }
        if (listenFD_ >= 0) {
            close(listenFD_);
        }
        unlink(kSocketPath);
        if (smcOpen_) {
            smc_reader_close(&smc_);
        }
    }

    bool initialize(std::string *error) {
        if (geteuid() != 0U) {
            return setError(error, "turbomacd must run as root");
        }
        if (!log_.openLog(error)) {
            return false;
        }
        if (!driver_.open(error)
            || !driver_.capabilities(&capabilities_, error)) {
            return false;
        }
        if (capabilities_.version != TURBOMAC_PROTOCOL_VERSION
            || capabilities_.size != sizeof(capabilities_)
            || (capabilities_.flags & kTurboMacCapabilitySupportedCPU) == 0U) {
            return setError(error, "TurboMac KEXT protocol or CPU capability mismatch");
        }

        char smcError[192] = {};
        if (!smc_reader_open(&smc_, smcError, sizeof(smcError))) {
            return setError(error, smcError);
        }
        smcOpen_ = true;
        const double now = monotonicSeconds();
        if (!sampleSMC(now, true, error)) {
            return false;
        }
        if (!ProfileStore::currentIdentity(
                pdtrType_, acpwType_, &identity_, error
            )) {
            return false;
        }

        loadProfile();
        configurePolicy();
        if (!createSocket(error)) {
            return false;
        }
        nextRAPLTime_ = now;
        nextSMCTime_ = now + kSMCFastInterval;
        log_.write(
            "daemon_started",
            profileValid_ ? "valid calibration profile loaded" : "passive; no valid profile"
        );
        return true;
    }

    int run() {
        while (!stopRequested) {
            tick(true, true);
            pollfd descriptor = {.fd = listenFD_, .events = POLLIN, .revents = 0};
            const int result = poll(&descriptor, 1, 50);
            if (result > 0 && (descriptor.revents & POLLIN) != 0) {
                handleConnection();
            } else if (result < 0 && errno != EINTR) {
                log_.write("fatal", "poll failed");
                break;
            }
        }
        std::string error;
        if (armed_) {
            disarmGovernor("daemon shutdown", &error);
        }
        log_.write("daemon_stopped", "clean shutdown");
        return 0;
    }

private:
    static bool setError(std::string *error, const std::string &message) {
        if (error != nullptr) {
            *error = message;
        }
        return false;
    }

    bool readSMCValue(
        const char key[5],
        double scale,
        double *value,
        std::string *type,
        std::string *error
    ) {
        SMCRawValue raw = {};
        char smcError[192] = {};
        double decoded = 0.0;
        if (!smc_reader_read(&smc_, key, &raw, smcError, sizeof(smcError))
            || !smc_decode_numeric(&raw, &decoded)
            || !std::isfinite(decoded * scale)) {
            return setError(error, smcError[0] == '\0'
                ? std::string("could not decode AppleSMC key ") + key
                : smcError);
        }
        *value = decoded * scale;
        if (type != nullptr) {
            *type = std::string(raw.type, 4U);
        }
        return true;
    }

    bool sampleSMC(double now, bool initial, std::string *error = nullptr) {
        double input = 0.0;
        double capacity = 0.0;
        double temperature = NAN;
        std::string pdtrType;
        std::string acpwType;
        std::string localError;
        bool success = readSMCValue("PDTR", 1.0, &input, &pdtrType, &localError)
            && readSMCValue("ACPW", 0.001, &capacity, &acpwType, &localError);
        bool temperatureValid = readSMCValue(
            "TC0D", 1.0, &temperature, nullptr, &localError
        );
        if (!temperatureValid) {
            temperatureValid = readSMCValue(
                "TC0P", 1.0, &temperature, nullptr, &localError
            );
        }
        if (success && (!std::isfinite(input) || input < 0.0 || input > 250.0
                        || !std::isfinite(capacity) || capacity < 10.0 || capacity > 250.0)) {
            success = false;
            localError = "AppleSMC power reading failed bounds validation";
        }
        if (success && !initial
            && (pdtrType != pdtrType_ || acpwType != acpwType_)) {
            success = false;
            localError = "AppleSMC key type changed";
        }
        if (!success || !policy_.updateSMC(now, input, capacity)) {
            smcFailures_++;
            smcSuccesses_ = 0U;
            smcErrorSerial_++;
            nextSMCTime_ = now + kSMCFallbackInterval;
            if (error != nullptr) {
                *error = localError.empty() ? "invalid AppleSMC sample" : localError;
            }
            log_.write("smc_invalid", localError.empty() ? "invalid sample" : localError);
            return false;
        }

        if (initial) {
            pdtrType_ = pdtrType;
            acpwType_ = acpwType;
        }
        latestInputW_ = input;
        latestCapacityW_ = capacity;
        latestTemperatureC_ = temperatureValid ? temperature : NAN;
        smcFailures_ = 0U;
        smcSuccesses_++;
        smcSampleSerial_++;
        nextSMCTime_ = now + (smcSuccesses_ >= kTelemetryRecoverySamples
            ? kSMCFastInterval
            : (smcErrorSerial_ == 0U ? kSMCFastInterval : kSMCFallbackInterval));
        return true;
    }

    bool sampleRAPL(double now, std::string *error = nullptr) {
        TurboMacTelemetry current = {};
        std::string localError;
        if (!driver_.telemetry(&current, &localError)
            || current.version != TURBOMAC_PROTOCOL_VERSION
            || current.size != sizeof(current)) {
            raplErrorSerial_++;
            if (error != nullptr) {
                *error = localError.empty() ? "invalid RAPL telemetry" : localError;
            }
            log_.write("rapl_invalid", localError.empty() ? "invalid telemetry" : localError);
            return false;
        }

        const uint32_t currentEnergyRaw = (uint32_t)current.package_energy_raw;
        const uint32_t previousEnergyRaw = energyTracker_.previousRaw();
        const double elapsed = now - energyTracker_.previousTime();
        double packageW = 0.0;
        const TurboMacEnergySampleResult energyResult = energyTracker_.sample(
            currentEnergyRaw,
            capabilities_.rapl_energy_exponent,
            now,
            &packageW
        );
        telemetry_ = current;
        if (energyResult == TurboMacEnergySampleResult::Invalid
            || (energyResult == TurboMacEnergySampleResult::Valid
                && !policy_.updatePackagePower(now, packageW))) {
            raplSuccesses_ = 0U;
            raplErrorSerial_++;
            nextRAPLTime_ = now + kRAPLFallbackInterval;
            if (error != nullptr) {
                *error = "RAPL energy delta failed validation";
            }
            std::ostringstream detail;
            detail << std::fixed << std::setprecision(3)
                   << "energy delta rejected elapsed_s=" << elapsed
                   << " delta_raw=" << (uint32_t)(currentEnergyRaw - previousEnergyRaw);
            if (energyResult == TurboMacEnergySampleResult::Valid) {
                detail << " package_w=" << packageW;
            }
            log_.write("rapl_invalid", detail.str());
            return false;
        }
        raplSuccesses_++;
        nextRAPLTime_ = now + (raplSuccesses_ >= kTelemetryRecoverySamples
            ? kRAPLSampleInterval
            : (raplErrorSerial_ == 0U ? kRAPLSampleInterval : kRAPLFallbackInterval));

        if (armed_ && current.driver_state != kTurboMacDriverArmed) {
            armed_ = false;
            validTelemetrySince_ = -1.0;
            log_.write("driver_restored_guard", "KEXT left armed state");
        }
        return true;
    }

    bool refreshCPUDieTemperature(std::string *error) {
        double temperature = 0.0;
        if (!TemperatureReader::readCPUDie(&temperature, error)) {
            return false;
        }
        latestCPUDieTemperatureC_ = temperature;
        lastCPUDieReadTime_ = monotonicSeconds();
        return true;
    }

    void sampleBattery(double now) {
        if (now >= nextBatteryTime_) {
            batteryPercent_ = readBatteryPercent();
            nextBatteryTime_ = now + kBatterySampleInterval;
        }
        policy_.updateBatteryState(batteryPercent_);
    }

    void tick(bool applyControl, bool allowAutoArm) {
        const double now = monotonicSeconds();
        sampleBattery(now);
        if (now >= nextRAPLTime_) {
            sampleRAPL(now);
        }
        if (now >= nextSMCTime_) {
            sampleSMC(now, false);
        }

        const PolicySnapshot snapshot = policy_.evaluate(now);
        if (snapshot.ready && smcFailures_ == 0U) {
            if (validTelemetrySince_ < 0.0) {
                validTelemetrySince_ = now;
            }
        } else {
            validTelemetrySince_ = -1.0;
        }

        if (armed_) {
            if (smcFailures_ >= 3U || !snapshot.ready) {
                std::string ignored;
                disarmGovernor(
                    smcFailures_ >= 3U ? "three invalid SMC samples" : "telemetry stale over two seconds",
                    &ignored
                );
            } else if (applyControl) {
                std::string error;
                if (!applyPolicy(snapshot, now, &error)) {
                    log_.write("control_failure", error);
                    driver_.close();
                    armed_ = false;
                    stopRequested = 1;
                }
            }
        } else if (allowAutoArm
                   && !operationActive_
                   && profileValid_
                   && profile_.autoArm
                   && validTelemetrySince_ >= 0.0
                   && now - validTelemetrySince_ >= kAutoArmTelemetrySeconds) {
            std::string error;
            if (!armGovernor("auto-arm", &error)) {
                log_.write("auto_arm_refused", error);
                validTelemetrySince_ = now;
            }
        }

        if (snapshot.band != lastLoggedBand_) {
            log_.write(
                "band_transition",
                std::string(GovernorPolicy::bandName(lastLoggedBand_)) + " -> "
                    + GovernorPolicy::bandName(snapshot.band)
            );
            lastLoggedBand_ = snapshot.band;
        }
        if (now - lastTelemetryLogTime_ >= 1.0 && snapshot.ready) {
            std::ostringstream detail;
            detail << std::fixed << std::setprecision(3)
                   << "band=" << GovernorPolicy::bandName(snapshot.band)
                   << " input_w=" << snapshot.inputW
                   << " filtered_input_w=" << snapshot.filteredInputW
                   << " package_w=" << snapshot.packageW
                   << " filtered_package_w=" << snapshot.filteredPackageW
                   << " aligned_package_w=" << snapshot.alignedPackageW
                   << " non_cpu_w=" << snapshot.nonCPUW
                   << " raw_non_cpu_observation_w=" << snapshot.rawNonCPUObservationW
                   << " non_cpu_observation_w=" << snapshot.nonCPUObservationW
                   << " predicted_input_w=" << snapshot.predictedInputW
                   << " capacity_w=" << snapshot.capacityW
                   << " desired_pl1_w=" << snapshot.desiredPL1W
                   << " pl1_w=" << snapshot.pl1W
                   << " pl2_w=" << snapshot.pl2W
                   << " recovery_interval_s=" << snapshot.recoveryIntervalS;
            if (snapshot.nonCPUOverBudget) {
                detail << " non_cpu_over_budget=true";
            }
            log_.write("telemetry", detail.str());
            lastTelemetryLogTime_ = now;
        }
    }

    bool applyPolicy(
        const PolicySnapshot &snapshot,
        double now,
        std::string *error
    ) {
        const uint32_t pl1MW = wattsToMW(snapshot.pl1W);
        const uint32_t pl2MW = wattsToMW(snapshot.pl2W);
        const bool changed = pl1MW != telemetry_.applied_pl1_mw
            || pl2MW != telemetry_.applied_pl2_mw;
        if (!changed && now - lastDriverUpdateTime_ < 1.0) {
            return true;
        }
        if (!driver_.update(pl1MW, pl2MW, error)) {
            return false;
        }
        lastDriverUpdateTime_ = now;
        return true;
    }

    bool passiveHWPReady(
        TurboMacHWPStatus *status,
        uint32_t *armMode,
        std::string *error
    ) {
        if (status == nullptr || armMode == nullptr
            || !driver_.hwpStatus(status, error)
            || status->version != TURBOMAC_PROTOCOL_VERSION
            || status->size != sizeof(*status)) {
            return setError(error, "could not read a compatible HWP status payload");
        }
        if ((status->flags & kTurboMacHWPStatusSupported) == 0U
            || (status->flags & kTurboMacHWPStatusOverrideActive) != 0U) {
            std::ostringstream detail;
            detail << "HWP precondition failed: flags=0x" << std::hex
                   << status->flags;
            return setError(error, detail.str());
        }
        if ((status->flags & kTurboMacHWPStatusEnabled) == 0U) {
            *armMode = kTurboMacHWPBootstrapAndReleaseMaximum;
            return true;
        }
        const uint32_t required = kTurboMacHWPStatusEnabled
            | kTurboMacHWPStatusMaximumRestricted
            | kTurboMacHWPStatusReadbackValid;
        const bool turboMacEnabled =
            (status->flags & kTurboMacHWPStatusEnabledByTurboMac) != 0U;
        const bool failSafeActive =
            (status->flags & kTurboMacHWPStatusFailsafeRequestActive) != 0U;
        if ((status->flags & required) != required
            || (turboMacEnabled && !failSafeActive)) {
            return setError(
                error,
                "enabled HWP is not in a readable, maximum-restricted passive state"
            );
        }
        *armMode = kTurboMacHWPReleaseMaximum;
        return true;
    }

    bool armedHWPVerified(std::string *error) {
        TurboMacHWPStatus status = {};
        if (!driver_.hwpStatus(&status, error)
            || status.version != TURBOMAC_PROTOCOL_VERSION
            || status.size != sizeof(status)) {
            return setError(error, "could not read a compatible armed HWP status payload");
        }
        const uint32_t required = kTurboMacHWPStatusSupported
            | kTurboMacHWPStatusEnabled
            | kTurboMacHWPStatusOverrideActive
            | kTurboMacHWPStatusCaptureValid
            | kTurboMacHWPStatusReadbackValid;
        if ((status.flags & required) != required
            || (status.hwp_mode != kTurboMacHWPReleaseMaximum
                && status.hwp_mode != kTurboMacHWPBootstrapAndReleaseMaximum)) {
            return setError(error, "HWP maximum-release readback did not match");
        }
        return true;
    }

    bool armGovernor(const std::string &reason, std::string *error) {
        const double now = monotonicSeconds();
        const PolicySnapshot snapshot = policy_.evaluate(now);
        if (!profileValid_) {
            return setError(error, "no valid calibration profile");
        }
        if (!snapshot.ready
            || validTelemetrySince_ < 0.0
            || now - validTelemetrySince_ < kAutoArmTelemetrySeconds) {
            return setError(error, "ten continuous seconds of valid telemetry are required");
        }
        TurboMacCapabilities currentCapabilities = {};
        if (!driver_.capabilities(&currentCapabilities, error)
            || (currentCapabilities.flags & kTurboMacCapabilityBidirProchotEnabled) == 0U
            || (currentCapabilities.flags & kTurboMacCapabilityLimitLocked) != 0U) {
            return setError(error, "Apple guard is not enabled or package RAPL is locked");
        }
        TurboMacHWPStatus passiveHWP = {};
        uint32_t hwpMode = kTurboMacHWPModeInvalid;
        if (!passiveHWPReady(&passiveHWP, &hwpMode, error)) {
            return false;
        }

        const uint32_t pl1MW = wattsToMW(snapshot.pl1W);
        const uint32_t pl2MW = wattsToMW(snapshot.pl2W);
        if (!driver_.arm(pl1MW, pl2MW, hwpMode, error)) {
            return false;
        }
        TurboMacDriverStatus status = {};
        if (!driver_.status(&status, error)
            || status.driver_state != kTurboMacDriverArmed
            || std::abs((int64_t)status.applied_pl1_mw - (int64_t)pl1MW) > 200
            || std::abs((int64_t)status.applied_pl2_mw - (int64_t)pl2MW) > 200
            || !armedHWPVerified(error)) {
            std::string ignored;
            driver_.disarm(&ignored);
            return setError(error, "RAPL arm readback did not match the requested limits");
        }
        armed_ = true;
        lastDriverUpdateTime_ = now;
        log_.write("governor_armed", reason);
        return true;
    }

    bool setManualLimits(double pl1W, double pl2W, std::string *error) {
        uint32_t hwpMode = kTurboMacHWPReleaseMaximum;
        if (!armed_) {
            TurboMacHWPStatus passiveHWP = {};
            if (!passiveHWPReady(&passiveHWP, &hwpMode, error)) {
                return false;
            }
        }
        const uint32_t pl1MW = wattsToMW(pl1W);
        const uint32_t pl2MW = wattsToMW(pl2W);
        const bool success = armed_
            ? driver_.update(pl1MW, pl2MW, error)
            : driver_.arm(pl1MW, pl2MW, hwpMode, error);
        if (!success) {
            return false;
        }
        TurboMacDriverStatus status = {};
        if (!driver_.status(&status, error)
            || status.driver_state != kTurboMacDriverArmed
            || std::abs((int64_t)status.applied_pl1_mw - (int64_t)pl1MW) > 200
            || std::abs((int64_t)status.applied_pl2_mw - (int64_t)pl2MW) > 200
            || !armedHWPVerified(error)) {
            std::string ignored;
            driver_.disarm(&ignored);
            return setError(error, "manual RAPL/HWP readback mismatch");
        }
        armed_ = true;
        lastDriverUpdateTime_ = monotonicSeconds();
        return true;
    }

    bool disarmGovernor(const std::string &reason, std::string *error) {
        bool success = true;
        if (driver_.isOpen()) {
            success = driver_.disarm(error);
        }
        armed_ = false;
        validTelemetrySince_ = -1.0;
        configurePolicy();
        log_.write("governor_disarmed", reason);
        return success;
    }

    void loadProfile() {
        struct stat status = {};
        if (lstat(ProfileStore::kPath, &status) != 0 && errno == ENOENT) {
            profileValid_ = false;
            return;
        }
        std::string error;
        profileValid_ = ProfileStore::load(&profile_, &error)
            && ProfileStore::matches(profile_, identity_, &error)
            && profileFitsCapabilities(profile_, &error);
        if (!profileValid_) {
            log_.write("profile_rejected", error);
        }
    }

    bool profileFitsCapabilities(
        const CalibrationProfile &profile,
        std::string *error
    ) const {
        const double driverFloor = (double)capabilities_.minimum_power_mw / 1000.0;
        const double driverMaximum = (double)capabilities_.maximum_power_mw / 1000.0;
        if (profile.raplFloorW + 0.2 < driverFloor
            || profile.raplCeilingPL1W > driverMaximum + 0.2
            || profile.raplCeilingPL2W > driverMaximum + 0.2) {
            return setError(error, "profile RAPL bounds do not fit driver capabilities");
        }
        return true;
    }

    void configurePolicy() {
        PolicyConfig config;
        const double currentPL1 = rawLimitW(
            capabilities_.current_package_limit_raw,
            false,
            capabilities_.rapl_power_exponent
        );
        const double currentPL2 = rawLimitW(
            capabilities_.current_package_limit_raw,
            true,
            capabilities_.rapl_power_exponent
        );
        config.raplFloorW = std::max(
            1.0, (double)capabilities_.minimum_power_mw / 1000.0
        );
        config.raplCeilingPL1W = currentPL1;
        config.raplCeilingPL2W = currentPL2;
        if (profileValid_) {
            config.reserveW = profile_.reserveW;
            config.raplFloorW = std::max(config.raplFloorW, profile_.raplFloorW);
            config.raplCeilingPL1W = std::min(currentPL1, profile_.raplCeilingPL1W);
            config.raplCeilingPL2W = std::min(currentPL2, profile_.raplCeilingPL2W);
            config.cruisePL2BurstW = profile_.cruisePL2BurstW;
            config.packageToInputSlope = profile_.packageToInputSlope;
            config.packageToInputInterceptW = profile_.packageToInputInterceptW;
            config.guardRatio = profile_.guardRatio;
            config.shedRatio = profile_.shedRatio;
            config.emergencyRatio = profile_.emergencyRatio;
            config.cruiseTargetRatio = profile_.cruiseTargetRatio;
            config.guardTargetRatio = profile_.guardTargetRatio;
            config.shedTargetRatio = profile_.shedTargetRatio;
            if (profile_.performanceMode) {
                // Schema-3 performance profile: fixed RAPL targets. Input power
                // becomes advisory telemetry; the live captured limits remain
                // the bounds the daemon can never exceed.
                config.fixedTargets = true;
                config.fixedPL1W = std::min(
                    profile_.performancePL1W, config.raplCeilingPL1W
                );
                config.fixedPL2W = std::min(
                    profile_.performancePL2W, config.raplCeilingPL2W
                );
            }
        }
        policy_.reset(config);
        energyTracker_.reset();
        raplSuccesses_ = 0U;
        validTelemetrySince_ = -1.0;
    }

    bool createSocket(std::string *error) {
        struct stat existing = {};
        if (lstat(kSocketPath, &existing) == 0) {
            if (!S_ISSOCK(existing.st_mode) || existing.st_uid != 0U) {
                return setError(error, "refusing to replace unsafe daemon socket path");
            }
            unlink(kSocketPath);
        }
        listenFD_ = socket(AF_UNIX, SOCK_STREAM, 0);
        if (listenFD_ < 0) {
            return setError(error, "could not create daemon socket");
        }
        sockaddr_un address = {};
        address.sun_family = AF_UNIX;
        std::snprintf(address.sun_path, sizeof(address.sun_path), "%s", kSocketPath);
        const mode_t previousMask = umask(0077);
        const int bindResult = bind(
            listenFD_, reinterpret_cast<sockaddr *>(&address), sizeof(address)
        );
        umask(previousMask);
        if (bindResult != 0 || chmod(kSocketPath, 0600) != 0 || listen(listenFD_, 4) != 0) {
            return setError(error, "could not bind protected daemon socket");
        }
        return true;
    }

    void handleConnection() {
        const int client = accept(listenFD_, nullptr, nullptr);
        if (client < 0) {
            return;
        }
        uid_t uid = (uid_t)-1;
        gid_t gid = (gid_t)-1;
        if (getpeereid(client, &uid, &gid) != 0 || uid != 0U) {
            writeAll(client, "ERROR root privileges required\n");
            close(client);
            return;
        }
        timeval timeout = {.tv_sec = 5, .tv_usec = 0};
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        std::string command;
        char buffer[256];
        while (command.size() <= 256U) {
            const ssize_t count = read(client, buffer, sizeof(buffer));
            if (count < 0 && errno == EINTR) {
                continue;
            }
            if (count <= 0) {
                break;
            }
            command.append(buffer, (size_t)count);
            if (command.find('\n') != std::string::npos) {
                break;
            }
        }
        const size_t newline = command.find('\n');
        if (newline != std::string::npos) {
            command.resize(newline);
        }
        if (command.size() > 256U) {
            writeAll(client, "ERROR command too long\n");
        } else {
            dispatchCommand(client, command);
        }
        close(client);
    }

    void dispatchCommand(int client, const std::string &command) {
        if (command == "status json") {
            writeAll(client, statusJSON() + "\n");
            return;
        }
        if (command == "status human") {
            writeAll(client, statusHuman());
            return;
        }
        if (command == "logs") {
            sendLogs(client);
            return;
        }
        if (operationActive_) {
            writeAll(client, "ERROR another supervised operation is active\n");
            return;
        }
        if (command == "arm") {
            std::string error;
            if (armed_) {
                writeAll(client, "OK governor is already armed\n");
            } else if (armGovernor("manual arm", &error)) {
                writeAll(client, "OK governor armed\n");
            } else {
                writeAll(client, "ERROR " + error + "\n");
            }
            return;
        }
        if (command == "disarm") {
            std::string error;
            if (disarmGovernor("manual disarm", &error)) {
                writeAll(client, "OK Apple guard restored\n");
            } else {
                writeAll(client, "ERROR " + error + "\n");
            }
            return;
        }
        if (command == "calibrate supervised") {
            operationActive_ = true;
            runCalibration(client);
            operationActive_ = false;
            return;
        }
        if (command == "validate supervised") {
            operationActive_ = true;
            runValidation(client);
            operationActive_ = false;
            return;
        }
        writeAll(client, "ERROR unknown command\n");
    }

    std::string statusJSON() {
        TurboMacDriverStatus driverStatus = {};
        TurboMacCapabilities statusCapabilities = {};
        TurboMacHWPStatus hwpStatus = {};
        std::string ignored;
        const bool driverStatusValid = driver_.status(&driverStatus, &ignored);
        const bool statusCapabilitiesValid = driver_.capabilities(
            &statusCapabilities,
            &ignored
        );
        const bool hwpStatusValid = driver_.hwpStatus(&hwpStatus, &ignored)
            && hwpStatus.version == TURBOMAC_PROTOCOL_VERSION
            && hwpStatus.size == sizeof(hwpStatus);
        const PolicySnapshot snapshot = policy_.evaluate(monotonicSeconds());
        std::ostringstream output;
        output << std::fixed << std::setprecision(3)
               << "{\"ok\":true"
               << ",\"armed\":" << (armed_ ? "true" : "false")
               << ",\"profile_valid\":" << (profileValid_ ? "true" : "false")
               << ",\"auto_arm\":" << (profileValid_ && profile_.autoArm ? "true" : "false")
               << ",\"validation_passed\":"
               << (profileValid_ && profile_.validationPassed ? "true" : "false")
               << ",\"telemetry_ready\":" << (snapshot.ready ? "true" : "false")
               << ",\"band\":\"" << GovernorPolicy::bandName(snapshot.band) << "\""
               << ",\"policy_mode\":\""
               << (snapshot.fixedTargets ? "performance" : "input_governed") << "\""
               << ",\"input_w\":" << snapshot.inputW
               << ",\"capacity_w\":" << snapshot.capacityW
               << ",\"package_w\":" << snapshot.packageW
               << ",\"filtered_package_w\":" << snapshot.filteredPackageW
               << ",\"aligned_package_w\":" << snapshot.alignedPackageW
               << ",\"non_cpu_w\":" << snapshot.nonCPUW
               << ",\"raw_non_cpu_observation_w\":" << snapshot.rawNonCPUObservationW
               << ",\"non_cpu_observation_w\":" << snapshot.nonCPUObservationW
               << ",\"filtered_input_w\":" << snapshot.filteredInputW
               << ",\"predicted_input_w\":" << snapshot.predictedInputW
               << ",\"guard_w\":" << snapshot.guardW
               << ",\"shed_w\":" << snapshot.shedW
               << ",\"emergency_w\":" << snapshot.emergencyW
               << ",\"state_target_w\":" << snapshot.stateTargetW
               << ",\"desired_pl1_w\":" << snapshot.desiredPL1W
               << ",\"pl1_w\":" << snapshot.pl1W
               << ",\"pl2_w\":" << snapshot.pl2W
               << ",\"recovery_interval_s\":" << snapshot.recoveryIntervalS
               << ",\"cpu_temp_c\":";
        if (std::isfinite(latestTemperatureC_)) {
            output << latestTemperatureC_;
        } else {
            output << "null";
        }
        output << ",\"cpu_die_temp_c\":";
        if (std::isfinite(latestCPUDieTemperatureC_)) {
            output << latestCPUDieTemperatureC_;
        } else {
            output << "null";
        }
        output << ",\"non_cpu_over_budget\":"
               << (snapshot.nonCPUOverBudget ? "true" : "false")
               << ",\"smc_invalid_consecutive\":" << smcFailures_
               << ",\"battery_percent\":";
        if (snapshot.batteryPercent >= 0.0) {
            output << snapshot.batteryPercent;
        } else {
            output << "null";
        }
        output << ",\"battery_buffered\":"
               << (snapshot.batteryBuffered ? "true" : "false")
               << ",\"apple_guard_enabled\":"
               << (statusCapabilitiesValid
                    && (statusCapabilities.flags & kTurboMacCapabilityBidirProchotEnabled) != 0U
                    ? "true" : "false")
               << ",\"rapl_locked\":"
               << (statusCapabilitiesValid
                    && (statusCapabilities.flags & kTurboMacCapabilityLimitLocked) != 0U
                    ? "true" : "false")
               << ",\"hwp_supported\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusSupported) != 0U
                    ? "true" : "false")
               << ",\"hwp_enabled\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusEnabled) != 0U
                    ? "true" : "false")
               << ",\"hwp_maximum_restricted\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusMaximumRestricted) != 0U
                    ? "true" : "false")
               << ",\"hwp_override_active\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusOverrideActive) != 0U
                    ? "true" : "false")
               << ",\"hwp_enabled_by_turbomac\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusEnabledByTurboMac) != 0U
                    ? "true" : "false")
               << ",\"hwp_native_restore_requires_reboot\":"
               << (hwpStatusValid
                    && (hwpStatus.flags
                        & kTurboMacHWPStatusNativeRestoreRequiresReboot) != 0U
                    ? "true" : "false")
               << ",\"hwp_failsafe_active\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusFailsafeRequestActive) != 0U
                    ? "true" : "false")
               << ",\"hwp_readback_valid\":"
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusReadbackValid) != 0U
                    ? "true" : "false")
               << ",\"hwp_status_flags\":"
               << (hwpStatusValid ? hwpStatus.flags : 0U)
               << ",\"hwp_logical_cpu_count\":"
               << (hwpStatusValid ? hwpStatus.logical_cpu_count : 0U)
               << ",\"hwp_capabilities_raw\":"
               << (hwpStatusValid ? hwpStatus.hwp_capabilities_raw : 0U)
               << ",\"hwp_package_request_raw\":"
               << (hwpStatusValid ? hwpStatus.current_package_request_raw : 0U)
               << ",\"hwp_failsafe_request_raw\":"
               << (hwpStatusValid ? hwpStatus.failsafe_request_raw : 0U)
               << ",\"driver_capability_flags\":"
               << (statusCapabilitiesValid ? statusCapabilities.flags : 0U)
               << ",\"driver_minimum_power_w\":"
               << (statusCapabilitiesValid
                    ? (double)statusCapabilities.minimum_power_mw / 1000.0
                    : 0.0)
               << ",\"driver_maximum_power_w\":"
               << (statusCapabilitiesValid
                    ? (double)statusCapabilities.maximum_power_mw / 1000.0
                    : 0.0)
               << ",\"driver_thermal_spec_power_w\":"
               << (statusCapabilitiesValid
                    ? (double)statusCapabilities.thermal_spec_power_mw / 1000.0
                    : 0.0)
               << ",\"driver_ceiling_pl1_w\":"
               << (driverStatusValid ? (double)driverStatus.ceiling_pl1_mw / 1000.0 : 0.0)
               << ",\"driver_ceiling_pl2_w\":"
               << (driverStatusValid ? (double)driverStatus.ceiling_pl2_mw / 1000.0 : 0.0)
               << ",\"driver_state\":"
               << (driverStatusValid ? driverStatus.driver_state : UINT32_MAX)
               << ",\"driver_restore_reason\":"
               << (driverStatusValid ? driverStatus.restore_reason : UINT32_MAX)
               << ",\"current_package_limit_raw\":"
               << (driverStatusValid ? driverStatus.current_package_limit_raw : 0U)
               << ",\"current_power_control_raw\":"
               << (driverStatusValid ? driverStatus.current_power_control_raw : 0U)
               << ",\"hwp_cpus\":[";
        bool firstHWPEntry = true;
        if (hwpStatusValid) {
            for (uint32_t index = 0U; index < TURBOMAC_MAX_LOGICAL_CPUS; ++index) {
                const TurboMacHWPEntry &entry = hwpStatus.cpus[index];
                if ((entry.flags & kTurboMacHWPEntryPresent) == 0U) {
                    continue;
                }
                if (!firstHWPEntry) {
                    output << ",";
                }
                firstHWPEntry = false;
                const bool flexible =
                    (hwpStatus.flags & kTurboMacHWPStatusFlexibleRequestFields) != 0U;
                const uint32_t effectiveMaximum = tm_hwp_effective_maximum(
                    entry.current_request_raw,
                    hwpStatus.current_package_request_raw,
                    flexible
                );
                output << "{\"cpu\":" << entry.cpu_index
                       << ",\"flags\":" << entry.flags
                       << ",\"captured_request_raw\":" << entry.captured_request_raw
                       << ",\"current_request_raw\":" << entry.current_request_raw
                       << ",\"minimum\":" << tm_hwp_minimum(entry.current_request_raw)
                       << ",\"maximum\":" << tm_hwp_maximum(entry.current_request_raw)
                       << ",\"desired\":" << tm_hwp_desired(entry.current_request_raw)
                       << ",\"epp\":" << tm_hwp_epp(entry.current_request_raw)
                       << ",\"package_control\":"
                       << (tm_hwp_package_control(entry.current_request_raw)
                           ? "true" : "false")
                       << ",\"maximum_valid\":"
                       << (tm_hwp_maximum_valid(entry.current_request_raw)
                           ? "true" : "false")
                       << ",\"effective_maximum\":" << effectiveMaximum
                       << "}";
            }
        }
        output << "]}";
        return output.str();
    }

    std::string statusHuman() {
        const PolicySnapshot snapshot = policy_.evaluate(monotonicSeconds());
        TurboMacHWPStatus hwpStatus = {};
        std::string ignored;
        const bool hwpStatusValid = driver_.hwpStatus(&hwpStatus, &ignored)
            && hwpStatus.version == TURBOMAC_PROTOCOL_VERSION
            && hwpStatus.size == sizeof(hwpStatus);
        uint32_t minimumEffectiveMaximum = UINT32_MAX;
        uint32_t maximumEffectiveMaximum = 0U;
        if (hwpStatusValid) {
            for (uint32_t index = 0U; index < TURBOMAC_MAX_LOGICAL_CPUS; ++index) {
                const TurboMacHWPEntry &entry = hwpStatus.cpus[index];
                if ((entry.flags & kTurboMacHWPEntryPresent) == 0U) {
                    continue;
                }
                const uint32_t effective = tm_hwp_effective_maximum(
                    entry.current_request_raw,
                    hwpStatus.current_package_request_raw,
                    (hwpStatus.flags & kTurboMacHWPStatusFlexibleRequestFields) != 0U
                );
                minimumEffectiveMaximum = std::min(minimumEffectiveMaximum, effective);
                maximumEffectiveMaximum = std::max(maximumEffectiveMaximum, effective);
            }
        }
        std::ostringstream batteryLine;
        if (snapshot.batteryPercent >= 0.0) {
            batteryLine << snapshot.batteryPercent << " %";
        } else {
            batteryLine << "unavailable";
        }
        if (snapshot.batteryBuffered) {
            batteryLine << " (buffered: battery covers adapter deficit)";
        }
        std::ostringstream output;
        output << std::fixed << std::setprecision(2)
               << "Governor: " << (armed_ ? "armed" : "passive") << "\n"
               << "Profile: " << (profileValid_ ? "valid" : "missing/invalid") << "\n"
               << "Validation: "
               << (profileValid_ && profile_.validationPassed ? "passed" : "not passed") << "\n"
               << "Auto-arm: "
               << (profileValid_ && profile_.autoArm ? "enabled" : "disabled") << "\n"
               << "Band: " << GovernorPolicy::bandName(snapshot.band) << "\n"
               << "Governor mode: "
               << (snapshot.fixedTargets
                    ? "performance (fixed RAPL targets)"
                    : "input-power governed")
               << "\n"
               << "Input / capacity: " << snapshot.inputW << " / " << snapshot.capacityW << " W\n"
               << "Package raw/filtered/aligned / non-CPU: " << snapshot.packageW << " / "
               << snapshot.filteredPackageW << " / " << snapshot.alignedPackageW
               << " / " << snapshot.nonCPUW << " W\n"
               << "Non-CPU observation raw/effective: "
               << snapshot.rawNonCPUObservationW << " / "
               << snapshot.nonCPUObservationW << " W\n"
               << "Filtered/predicted input: " << snapshot.filteredInputW << " / "
               << snapshot.predictedInputW << " W\n"
               << "Bands guard/shed/emergency: " << snapshot.guardW << " / "
               << snapshot.shedW << " / " << snapshot.emergencyW << " W\n"
               << "Battery: " << batteryLine.str() << "\n"
               << "Desired/requested PL1 / PL2: " << snapshot.desiredPL1W << " / "
               << snapshot.pl1W << " / " << snapshot.pl2W << " W\n"
               << "PL1 recovery interval: " << snapshot.recoveryIntervalS << " s\n"
               << "HWP: "
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusEnabled) != 0U
                    ? "enabled" : "unavailable")
               << ", maximum "
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusMaximumRestricted) != 0U
                    ? "restricted" : "released/unverified")
               << ", override "
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusOverrideActive) != 0U
                    ? "active" : "inactive")
               << ", fail-safe "
               << (hwpStatusValid
                    && (hwpStatus.flags & kTurboMacHWPStatusFailsafeRequestActive) != 0U
                    ? "active" : "inactive")
               << ", effective max "
               << (minimumEffectiveMaximum == UINT32_MAX ? 0U : minimumEffectiveMaximum)
               << ".." << maximumEffectiveMaximum
               << "\n"
               << "Native HWP restore requires reboot: "
               << (hwpStatusValid
                    && (hwpStatus.flags
                        & kTurboMacHWPStatusNativeRestoreRequiresReboot) != 0U
                    ? "yes" : "no")
               << "\n"
               << "CPU temperature (SMC / die): " << latestTemperatureC_ << " / "
               << latestCPUDieTemperatureC_ << " C\n"
               << "SMC failures: " << smcFailures_ << " consecutive\n";
        return output.str();
    }

    void sendLogs(int client) {
        const int descriptor = ::open(kLogPath, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (descriptor < 0) {
            writeAll(client, "ERROR log is unavailable\n");
            return;
        }
        struct stat status = {};
        fstat(descriptor, &status);
        const off_t start = status.st_size > 256 * 1024 ? status.st_size - 256 * 1024 : 0;
        lseek(descriptor, start, SEEK_SET);
        std::string content;
        char buffer[4096];
        for (;;) {
            const ssize_t count = read(descriptor, buffer, sizeof(buffer));
            if (count <= 0) {
                break;
            }
            content.append(buffer, (size_t)count);
        }
        close(descriptor);
        if (start > 0) {
            const size_t firstNewline = content.find('\n');
            if (firstNewline != std::string::npos) {
                content.erase(0U, firstNewline + 1U);
            }
        }
        writeAll(client, content);
    }

    bool waitForValidTelemetry(int client, double seconds, std::string *error) {
        const double started = monotonicSeconds();
        double validSince = -1.0;
        unsigned lastReported = UINT32_MAX;
        while (!stopRequested && monotonicSeconds() - started < seconds + 30.0) {
            tick(false, false);
            const double now = monotonicSeconds();
            const PolicySnapshot snapshot = policy_.evaluate(now);
            if (snapshot.ready && smcFailures_ == 0U) {
                if (validSince < 0.0) {
                    validSince = now;
                }
                const unsigned elapsed = (unsigned)(now - validSince);
                if (elapsed != lastReported) {
                    if (!writeAll(
                            client,
                            "telemetry gate " + std::to_string(elapsed) + "/"
                                + std::to_string((unsigned)seconds) + " seconds\n"
                        )) {
                        return setError(error, "supervising client disconnected");
                    }
                    lastReported = elapsed;
                }
                if (now - validSince >= seconds) {
                    validTelemetrySince_ = validSince;
                    return true;
                }
            } else {
                validSince = -1.0;
            }
            sleepMilliseconds(50U);
        }
        return setError(error, "could not establish continuous valid telemetry");
    }

    LoadResult runLoad(
        int client,
        unsigned seconds,
        double pl1W,
        double pl2W,
        double baselineCapacityW,
        double softInputW,
        LoadStatistics *statistics,
        std::string *error
    ) {
        if (!setManualLimits(pl1W, pl2W, error)) {
            return LoadResult::Abort;
        }
        if (!refreshCPUDieTemperature(error)) {
            return LoadResult::Abort;
        }
        if (latestCPUDieTemperatureC_ >= kMaximumCPUTemperatureC) {
            return setError(error, "CPU die temperature reached the 95 C abort threshold"),
                LoadResult::Abort;
        }
        if (!protectedExecutable(kAVX2LoadPath, error)) {
            return LoadResult::Abort;
        }
        pid_t child = -1;
        if (!spawnProgram(
                kAVX2LoadPath,
                {"--seconds", std::to_string(seconds), "--threads", "8"},
                &child,
                error
            )) {
            return LoadResult::Abort;
        }

        const double started = monotonicSeconds();
        double lastHeartbeat = started;
        double lastProgress = started;
        uint64_t priorSMCError = smcErrorSerial_;
        uint64_t priorRAPLError = raplErrorSerial_;
        uint64_t priorSMCSample = smcSampleSerial_;
        int childStatus = 0;
        for (;;) {
            tick(false, false);
            const double now = monotonicSeconds();
            if (smcErrorSerial_ != priorSMCError || raplErrorSerial_ != priorRAPLError) {
                *error = "telemetry read failed during supervised load";
                terminateChild(child);
                return LoadResult::Abort;
            }
            const PolicySnapshot snapshot = policy_.evaluate(now);
            if (!snapshot.ready) {
                *error = "telemetry became stale during supervised load";
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (latestInputW_ >= kHardCalibrationInputW) {
                *error = "PDTR reached the 52 W calibration abort threshold";
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (now - lastCPUDieReadTime_ >= 1.0
                && !refreshCPUDieTemperature(error)) {
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (std::isfinite(latestCPUDieTemperatureC_)
                && latestCPUDieTemperatureC_ >= kMaximumCPUTemperatureC) {
                *error = "CPU temperature reached the 95 C calibration abort threshold";
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (!std::isfinite(latestCPUDieTemperatureC_)) {
                *error = "CPU temperature telemetry failed during calibration";
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (baselineCapacityW <= 0.0
                || std::abs(latestCapacityW_ - baselineCapacityW) / baselineCapacityW > 0.05) {
                *error = "ACPW changed by more than five percent during calibration";
                terminateChild(child);
                return LoadResult::Abort;
            }
            if (latestInputW_ >= softInputW) {
                terminateChild(child);
                return LoadResult::SoftCeiling;
            }

            if (smcSampleSerial_ != priorSMCSample
                && now - started >= kCalibrationStatisticsWarmupSeconds) {
                statistics->inputTotal += latestInputW_;
                statistics->packageTotal += snapshot.filteredPackageW;
                statistics->peakInput = std::max(statistics->peakInput, latestInputW_);
                statistics->peakTemperature = std::max(
                    statistics->peakTemperature, latestCPUDieTemperatureC_
                );
                statistics->worstTransientError = std::max(
                    statistics->worstTransientError,
                    latestInputW_ - snapshot.filteredPackageW - snapshot.nonCPUW
                );
                statistics->samples++;
                priorSMCSample = smcSampleSerial_;
            }

            if (now - lastHeartbeat >= 1.0) {
                if (!setManualLimits(pl1W, pl2W, error)) {
                    terminateChild(child);
                    return LoadResult::Abort;
                }
                lastHeartbeat = now;
            }
            if (now - lastProgress >= 5.0) {
                std::ostringstream progress;
                progress << std::fixed << std::setprecision(2)
                         << "load " << (unsigned)(now - started) << "/" << seconds
                         << "s PL1=" << pl1W << " PL2=" << pl2W
                         << " PDTR=" << latestInputW_
                         << " temp=" << latestCPUDieTemperatureC_ << "\n";
                if (!writeAll(client, progress.str())) {
                    *error = "supervising client disconnected";
                    terminateChild(child);
                    return LoadResult::Abort;
                }
                lastProgress = now;
            }

            const pid_t waited = waitpid(child, &childStatus, WNOHANG);
            if (waited == child) {
                break;
            }
            if (waited < 0 && errno != EINTR) {
                *error = "could not monitor AVX2 load worker";
                terminateChild(child);
                return LoadResult::Abort;
            }
            sleepMilliseconds(50U);
        }
        if (!WIFEXITED(childStatus) || WEXITSTATUS(childStatus) != 0) {
            return setError(error, "AVX2 load worker failed"), LoadResult::Abort;
        }
        if (statistics->samples < (size_t)seconds * 2U) {
            return setError(error, "insufficient calibration samples"), LoadResult::Abort;
        }
        return LoadResult::Complete;
    }

    void runCalibration(int client) {
        std::string error;
        if (!writeAll(
                client,
                "Calibration is bounded at PDTR 52 W and 95 C; conservative stop begins at 50 W.\n"
            )) {
            return;
        }
        if (armed_ && !disarmGovernor("prepare calibration", &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        if (!waitForValidTelemetry(client, kAutoArmTelemetrySeconds, &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }

        TurboMacCapabilities current = {};
        if (!driver_.capabilities(&current, &error)) {
            writeAll(client, "ERROR could not read current driver capabilities: " + error + "\n");
            return;
        }
        const bool appleGuardEnabled =
            (current.flags & kTurboMacCapabilityBidirProchotEnabled) != 0U;
        const bool raplLocked =
            (current.flags & kTurboMacCapabilityLimitLocked) != 0U;
        if (!appleGuardEnabled || raplLocked) {
            writeAll(
                client,
                std::string("ERROR calibration precondition failed: apple_guard_enabled=")
                    + (appleGuardEnabled ? "true" : "false")
                    + " rapl_locked=" + (raplLocked ? "true" : "false") + "\n"
            );
            return;
        }
        TurboMacHWPStatus passiveHWP = {};
        uint32_t calibrationHWPMode = kTurboMacHWPModeInvalid;
        if (!passiveHWPReady(&passiveHWP, &calibrationHWPMode, &error)) {
            writeAll(client, "ERROR calibration precondition failed: " + error + "\n");
            return;
        }
        {
            std::ostringstream hwpDetail;
            if (calibrationHWPMode == kTurboMacHWPBootstrapAndReleaseMaximum) {
                hwpDetail
                    << "HWP is disabled. The first 5 W calibration arm will install "
                    << "and verify RAPL with Apple's guard still enabled, then enable "
                    << "HWP for this boot. Native restoration will require a reboot.\n";
            } else {
                hwpDetail << "Conservative HWP maximum confirmed across "
                          << passiveHWP.logical_cpu_count
                          << " logical CPUs; highest=0x" << std::hex
                          << (passiveHWP.hwp_capabilities_raw & UINT64_C(0xff))
                          << "\n";
            }
            if (!writeAll(client, hwpDetail.str())) {
                return;
            }
        }
        const double driverFloor = std::max(
            1.0, (double)current.minimum_power_mw / 1000.0
        );
        const double capturedPL1 = rawLimitW(
            current.current_package_limit_raw, false, current.rapl_power_exponent
        );
        const double capturedPL2 = rawLimitW(
            current.current_package_limit_raw, true, current.rapl_power_exponent
        );
        const double driverMaximum = (double)current.maximum_power_mw / 1000.0;
        const double sweepCeiling = std::min(
            std::min(capturedPL1, capturedPL2), driverMaximum
        );
        double limit = std::ceil(std::max(driverFloor, 5.0));
        if (sweepCeiling < limit) {
            writeAll(client, "ERROR captured RAPL ceiling is below calibration floor\n");
            return;
        }

        const double baselineCapacity = policy_.snapshot().capacityW;
        std::vector<TurboMacCalibrationPoint> mapping;
        double responsiveFloor = 0.0;
        double lastSafeLimit = 0.0;
        double worstError = 0.0;
        bool stopSweep = false;

        while (limit <= sweepCeiling + 0.01 && !stopSweep && !stopRequested) {
            LoadStatistics step;
            if (!writeAll(client, "testing PL1=PL2=" + std::to_string(limit) + " W\n")) {
                error = "supervising client disconnected";
                break;
            }
            for (unsigned run = 1U; run <= 3U; ++run) {
                if (!writeAll(client, "run " + std::to_string(run) + "/3\n")) {
                    error = "supervising client disconnected";
                    break;
                }
                LoadStatistics currentRun;
                const LoadResult result = runLoad(
                    client,
                    30U,
                    limit,
                    limit,
                    baselineCapacity,
                    kSoftCalibrationInputW,
                    &currentRun,
                    &error
                );
                if (result == LoadResult::Abort) {
                    stopSweep = true;
                    break;
                }
                if (result == LoadResult::SoftCeiling) {
                    stopSweep = true;
                    error.clear();
                    writeAll(client, "conservative 50 W stop reached; discarding this step\n");
                    break;
                }
                step.inputTotal += currentRun.inputTotal;
                step.packageTotal += currentRun.packageTotal;
                step.samples += currentRun.samples;
                step.peakInput = std::max(step.peakInput, currentRun.peakInput);
                step.peakTemperature = std::max(
                    step.peakTemperature, currentRun.peakTemperature
                );
                step.worstTransientError = std::max(
                    step.worstTransientError, currentRun.worstTransientError
                );
            }
            if (stopSweep) {
                break;
            }

            const double averagePackage = step.averagePackage();
            const double averageInput = step.averageInput();
            if (!turboMacCalibrationTierResponsive(limit, averagePackage)) {
                if (responsiveFloor == 0.0
                    && turboMacCalibrationBelowEffectiveFloor(
                        limit, averagePackage
                    )) {
                    std::ostringstream floorProbe;
                    floorProbe << std::fixed << std::setprecision(2)
                               << "RAPL request " << limit
                               << " W is below the effective loaded package floor: "
                               << "avg package=" << averagePackage
                               << "; continuing upward\n";
                    writeAll(client, floorProbe.str());
                    log_.write("calibration_floor_probe", floorProbe.str());
                    limit += 2.0;
                    continue;
                }
                std::ostringstream plateau;
                plateau << std::fixed << std::setprecision(2)
                        << "RAPL response plateau at " << limit
                        << " W: avg package=" << averagePackage
                        << "; discarding this tier and stopping the sweep\n";
                writeAll(client, plateau.str());
                log_.write("calibration_plateau", plateau.str());
                break;
            }
            mapping.push_back({limit, averagePackage, averageInput});
            worstError = std::max(worstError, step.worstTransientError);
            lastSafeLimit = limit;
            if (responsiveFloor == 0.0) {
                responsiveFloor = limit;
            }
            std::ostringstream result;
            result << std::fixed << std::setprecision(2)
                   << "accepted " << limit << " W: avg input=" << averageInput
                   << " peak input=" << step.peakInput
                   << " avg package=" << averagePackage << "\n";
            if (!writeAll(client, result.str())) {
                error = "supervising client disconnected";
                break;
            }
            if (step.peakInput + 2.0 >= kSoftCalibrationInputW) {
                break;
            }
            limit += 2.0;
        }

        if (!error.empty() || responsiveFloor == 0.0 || lastSafeLimit == 0.0) {
            std::string ignored;
            disarmGovernor("calibration aborted", &ignored);
            writeAll(client, "ERROR " + (error.empty()
                ? "calibration did not establish a responsive safe range"
                : error) + "\n");
            log_.write("calibration_aborted", error);
            return;
        }

        const double reserve = std::max(
            2.0, std::ceil((std::max(0.0, worstError) + 0.5) * 2.0) / 2.0
        );
        const PolicySnapshot latest = policy_.evaluate(monotonicSeconds());
        const double burstBase = turboMacCalibrationBurstBase(
            mapping, latest.guardW, reserve
        );
        double selectedBurst = 0.0;
        if (burstBase <= 0.0) {
            writeAll(
                client,
                "no completed mapping point left reserve below guard; PL2 burst disabled\n"
            );
        } else {
            writeAll(
                client,
                "testing PL2 from mapped cruise base "
                    + std::to_string(burstBase) + " W\n"
            );
            const double burstMaximum = std::min(10.0, capturedPL2 - burstBase);
            for (double burst = 2.0; burst <= burstMaximum + 0.01; burst += 2.0) {
                bool accepted = true;
                for (unsigned run = 1U; run <= 3U; ++run) {
                    LoadStatistics burstRun;
                    const LoadResult result = runLoad(
                        client,
                        5U,
                        burstBase,
                        burstBase + burst,
                        baselineCapacity,
                        kSoftCalibrationInputW,
                        &burstRun,
                        &error
                    );
                    if (result != LoadResult::Complete) {
                        accepted = false;
                        break;
                    }
                    worstError = std::max(worstError, burstRun.worstTransientError);
                }
                if (!accepted) {
                    if (!error.empty()) {
                        std::string ignored;
                        disarmGovernor("PL2 calibration aborted", &ignored);
                        writeAll(client, "ERROR " + error + "\n");
                        return;
                    }
                    break;
                }
                selectedBurst = burst;
                writeAll(
                    client,
                    "accepted PL2 burst allowance " + std::to_string(burst) + " W\n"
                );
            }
        }

        std::string ignored;
        if (!disarmGovernor("calibration complete", &error)) {
            writeAll(client, "ERROR could not verify Apple guard restoration: " + error + "\n");
            return;
        }

        double slope = 1.0;
        double intercept = mapping.front().averageInputW
            - mapping.front().averagePackageW;
        if (mapping.size() >= 2U) {
            double sumX = 0.0;
            double sumY = 0.0;
            double sumXY = 0.0;
            double sumXX = 0.0;
            for (const auto &point : mapping) {
                sumX += point.averagePackageW;
                sumY += point.averageInputW;
                sumXY += point.averagePackageW * point.averageInputW;
                sumXX += point.averagePackageW * point.averagePackageW;
            }
            const double count = (double)mapping.size();
            const double denominator = count * sumXX - sumX * sumX;
            if (std::abs(denominator) > 1e-9) {
                slope = (count * sumXY - sumX * sumY) / denominator;
                intercept = (sumY - slope * sumX) / count;
            }
        }
        slope = std::max(0.25, std::min(4.0, slope));
        intercept = std::max(-20.0, std::min(100.0, intercept));

        CalibrationProfile profile;
        profile.identity = identity_;
        profile.calibratedCapacityW = baselineCapacity;
        profile.reserveW = std::max(
            reserve,
            std::ceil((std::max(0.0, worstError) + 0.5) * 2.0) / 2.0
        );
        profile.raplFloorW = responsiveFloor;
        profile.raplCeilingPL1W = lastSafeLimit;
        profile.raplCeilingPL2W = turboMacCalibrationPL2Ceiling(
            lastSafeLimit, burstBase, selectedBurst, capturedPL2
        );
        profile.cruisePL2BurstW = selectedBurst;
        profile.packageToInputSlope = slope;
        profile.packageToInputInterceptW = intercept;
        profile.calibrationPointCount = (uint32_t)mapping.size();
        profile.validationPassed = false;
        profile.autoArm = false;
        profile.calibratedAt = (int64_t)std::time(nullptr);
        profile.validatedAt = 0;
        if (!ProfileStore::save(profile, &error)) {
            writeAll(client, "ERROR could not save calibration profile: " + error + "\n");
            return;
        }
        profile_ = profile;
        profileValid_ = true;
        configurePolicy();
        log_.write("calibration_complete", "profile saved; validation required");
        std::ostringstream completed;
        completed << std::fixed << std::setprecision(2)
                  << "OK calibration saved: floor=" << responsiveFloor
                  << " PL1 ceiling=" << lastSafeLimit
                  << " reserve=" << profile.reserveW
                  << " PL2 burst=" << selectedBurst
                  << ". Auto-arm remains disabled until validate passes.\n";
        writeAll(client, completed.str());
    }

    void runValidation(int client) {
        std::string error;
        if (!profileValid_) {
            writeAll(client, "ERROR a valid calibration profile is required\n");
            return;
        }
        if (!protectedExecutable(kWhisperValidationPath, &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        if (armed_ && !disarmGovernor("prepare validation", &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        if (!waitForValidTelemetry(client, kAutoArmTelemetrySeconds, &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        if (!armed_ && !armGovernor("Whisper validation", &error)) {
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        if (!refreshCPUDieTemperature(&error)) {
            std::string ignored;
            disarmGovernor("validation temperature read failed", &ignored);
            writeAll(client, "ERROR " + error + "\n");
            return;
        }

        pid_t child = -1;
        if (!spawnProgram(kWhisperValidationPath, {}, &child, &error)) {
            std::string ignored;
            disarmGovernor("validation spawn failed", &ignored);
            writeAll(client, "ERROR " + error + "\n");
            return;
        }
        const PolicySnapshot initialValidationSnapshot = policy_.snapshot();
        if (!initialValidationSnapshot.ready
            || !std::isfinite(initialValidationSnapshot.shedW)
            || initialValidationSnapshot.shedW <= 0.0) {
            std::string ignored;
            disarmGovernor("validation boundary unavailable", &ignored);
            writeAll(client, "ERROR live validation boundary is unavailable\n");
            return;
        }
        {
            std::ostringstream startedMessage;
            startedMessage << std::fixed << std::setprecision(2)
                           << "Whisper validation started; PDTR abort follows the live shed "
                           << "threshold (" << initialValidationSnapshot.shedW
                           << " W) / 95 C.\n";
            writeAll(client, startedMessage.str());
        }
        const double started = monotonicSeconds();
        double lastProgress = started;
        const uint64_t initialSMCError = smcErrorSerial_;
        const uint64_t initialRAPLError = raplErrorSerial_;
        int childStatus = 0;
        bool failed = false;
        while (!stopRequested) {
            tick(true, false);
            const double now = monotonicSeconds();
            if (now - lastCPUDieReadTime_ >= 1.0
                && !refreshCPUDieTemperature(&error)) {
                failed = true;
                terminateChild(child);
                break;
            }
            const PolicySnapshot validationSnapshot = policy_.snapshot();
            bool boundaryViolation = false;
            if (!armed_) {
                error = "governor disarmed during validation";
                boundaryViolation = true;
            } else if (smcErrorSerial_ != initialSMCError) {
                error = "SMC telemetry failed during validation";
                boundaryViolation = true;
            } else if (raplErrorSerial_ != initialRAPLError) {
                error = "RAPL telemetry failed during validation";
                boundaryViolation = true;
            } else if (!validationSnapshot.ready
                || !std::isfinite(validationSnapshot.shedW)
                || validationSnapshot.shedW <= 0.0) {
                error = "live validation boundary became unavailable";
                boundaryViolation = true;
            } else if (latestInputW_ >= validationSnapshot.shedW) {
                std::ostringstream boundaryError;
                boundaryError << std::fixed << std::setprecision(2)
                              << "PDTR reached the live shed threshold ("
                              << validationSnapshot.shedW << " W)";
                error = boundaryError.str();
                boundaryViolation = true;
            } else if (!std::isfinite(latestCPUDieTemperatureC_)) {
                error = "CPU temperature became invalid during validation";
                boundaryViolation = true;
            } else if (latestCPUDieTemperatureC_ >= kMaximumCPUTemperatureC) {
                error = "CPU temperature reached the 95 C validation abort threshold";
                boundaryViolation = true;
            } else if (now - started > 600.0) {
                error = "validation exceeded the 600 second timeout";
                boundaryViolation = true;
            }
            if (boundaryViolation) {
                failed = true;
                terminateChild(child);
                break;
            }
            if (now - lastProgress >= 5.0) {
                const PolicySnapshot snapshot = policy_.snapshot();
                std::ostringstream progress;
                progress << std::fixed << std::setprecision(2)
                         << "validation " << (unsigned)(now - started)
                         << "s band=" << GovernorPolicy::bandName(snapshot.band)
                         << " PDTR=" << latestInputW_
                         << " PL1=" << snapshot.pl1W << "\n";
                if (!writeAll(client, progress.str())) {
                    failed = true;
                    error = "supervising client disconnected";
                    terminateChild(child);
                    break;
                }
                lastProgress = now;
            }
            const pid_t waited = waitpid(child, &childStatus, WNOHANG);
            if (waited == child) {
                break;
            }
            if (waited < 0 && errno != EINTR) {
                failed = true;
                error = "could not monitor Whisper validation";
                terminateChild(child);
                break;
            }
            sleepMilliseconds(50U);
        }
        if (!failed && (!WIFEXITED(childStatus) || WEXITSTATUS(childStatus) != 0)) {
            failed = true;
            error = "Whisper validation workload failed";
        }
        if (failed) {
            std::string ignored;
            disarmGovernor("validation failed", &ignored);
            writeAll(client, "ERROR " + error + "\n");
            log_.write("validation_failed", error);
            return;
        }

        profile_.validationPassed = true;
        profile_.autoArm = true;
        profile_.validatedAt = (int64_t)std::time(nullptr);
        if (!ProfileStore::save(profile_, &error)) {
            std::string ignored;
            disarmGovernor("validation profile save failed", &ignored);
            writeAll(client, "ERROR validation passed but profile save failed: " + error + "\n");
            return;
        }
        log_.write("validation_complete", "fixed Whisper workload passed; auto-arm enabled");
        writeAll(client, "OK Whisper validation passed; safe auto-arm enabled\n");
    }

    DriverClient driver_;
    SMCReader smc_;
    bool smcOpen_;
    GovernorPolicy policy_;
    EventLog log_;
    TurboMacCapabilities capabilities_;
    TurboMacTelemetry telemetry_;
    MachineIdentity identity_;
    CalibrationProfile profile_;
    bool profileValid_;
    bool armed_;
    bool operationActive_;
    int listenFD_;
    double nextRAPLTime_;
    double nextSMCTime_;
    double nextBatteryTime_;
    double batteryPercent_;
    double lastTelemetryLogTime_;
    double lastDriverUpdateTime_;
    double validTelemetrySince_;
    TurboMacEnergyTracker energyTracker_;
    double latestTemperatureC_;
    double latestCPUDieTemperatureC_;
    double lastCPUDieReadTime_;
    double latestCapacityW_;
    double latestInputW_;
    uint32_t smcFailures_;
    uint32_t smcSuccesses_;
    uint64_t smcErrorSerial_;
    uint32_t raplSuccesses_;
    uint64_t raplErrorSerial_;
    uint64_t smcSampleSerial_;
    GovernorBand lastLoggedBand_;
    std::string pdtrType_;
    std::string acpwType_;
};

} // namespace

int main() {
    std::signal(SIGINT, handleSignal);
    std::signal(SIGTERM, handleSignal);
    std::signal(SIGPIPE, SIG_IGN);
    TurboMacDaemon daemon;
    std::string error;
    if (!daemon.initialize(&error)) {
        std::fprintf(stderr, "turbomacd: %s\n", error.c_str());
        return 1;
    }
    return daemon.run();
}
