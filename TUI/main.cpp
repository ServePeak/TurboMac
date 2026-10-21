#include "TurboMacTopCore.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <immintrin.h>
#include <mach/mach.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <termios.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

constexpr const char *kSocketPath = "/var/run/turbomacd.sock";
constexpr unsigned kMaximumWorkers = 8U;
constexpr double kStatusStaleSeconds = 2.0;
constexpr double kFrequencyStaleSeconds = 3.0;
constexpr std::size_t kHistorySamples = 240U;

constexpr const char *kReset = "\x1b[0m";
constexpr const char *kBold = "\x1b[1m";
constexpr const char *kDim = "\x1b[2m";
constexpr const char *kGreen = "\x1b[38;5;46m";
constexpr const char *kCyan = "\x1b[38;5;51m";
constexpr const char *kAmber = "\x1b[38;5;220m";
constexpr const char *kOrange = "\x1b[38;5;208m";
constexpr const char *kRed = "\x1b[38;5;196m";
constexpr const char *kWhite = "\x1b[38;5;255m";
constexpr const char *kGray = "\x1b[38;5;244m";
constexpr const char *kBgGreen = "\x1b[30;48;5;46m";
constexpr const char *kBgAmber = "\x1b[30;48;5;220m";
constexpr const char *kBgRed = "\x1b[97;48;5;196m";
constexpr const char *kBgDark = "\x1b[97;48;5;236m";

volatile sig_atomic_t stopRequested = 0;
volatile double resultSinks[kMaximumWorkers] = {};

void handleSignal(int) {
    stopRequested = 1;
}

double monotonicSeconds() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration<double>(now).count();
}

int64_t monotonicNanoseconds() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(now).count();
}

std::string fixed(double value, int precision = 1) {
    if (!std::isfinite(value)) {
        return "---";
    }
    std::ostringstream stream;
    stream << std::fixed << std::setprecision(precision) << value;
    return stream.str();
}

std::string upper(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char character) {
        return static_cast<char>(std::toupper(character));
    });
    return value;
}

std::string colorForBand(const std::string &band) {
    if (band == "emergency") {
        return kRed;
    }
    if (band == "shed") {
        return kOrange;
    }
    if (band == "guard") {
        return kAmber;
    }
    return kGreen;
}

std::string lamp(const std::string &label, bool lit, const char *litColor = kBgGreen) {
    return std::string(lit ? litColor : kBgDark)
        + kBold + " " + label + " " + kReset;
}

class TerminalSession {
public:
    TerminalSession() {
        inputFD_ = open("/dev/tty", O_RDONLY | O_NOCTTY);
        if (inputFD_ >= 0) {
            ownsInputFD_ = true;
            fcntl(inputFD_, F_SETFD, FD_CLOEXEC);
        } else {
            inputFD_ = STDIN_FILENO;
        }
        if (!isatty(inputFD_) || !isatty(STDOUT_FILENO)) {
            error_ = "turbomactop requires an interactive terminal";
            closeOwnedInput();
            return;
        }
        if (tcgetattr(inputFD_, &original_) != 0) {
            error_ = "could not read terminal settings";
            closeOwnedInput();
            return;
        }
        termios raw = original_;
        raw.c_lflag &= static_cast<tcflag_t>(~(ICANON | ECHO));
        raw.c_iflag &= static_cast<tcflag_t>(~(IXON | ICRNL));
        raw.c_cc[VMIN] = 0;
        raw.c_cc[VTIME] = 0;
        if (tcsetattr(inputFD_, TCSAFLUSH, &raw) != 0) {
            error_ = "could not enter terminal raw mode";
            closeOwnedInput();
            return;
        }
        originalFlags_ = fcntl(inputFD_, F_GETFL, 0);
        if (originalFlags_ >= 0) {
            fcntl(inputFD_, F_SETFL, originalFlags_ | O_NONBLOCK);
        }
        active_ = true;
        std::cout << "\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H" << std::flush;
    }

    ~TerminalSession() {
        if (!active_) {
            return;
        }
        tcsetattr(inputFD_, TCSAFLUSH, &original_);
        if (originalFlags_ >= 0) {
            fcntl(inputFD_, F_SETFL, originalFlags_);
        }
        closeOwnedInput();
        std::cout << kReset << "\x1b[?25h\x1b[?1049l" << std::flush;
    }

    bool active() const {
        return active_;
    }

    const std::string &error() const {
        return error_;
    }

    int inputFD() const {
        return inputFD_;
    }

    std::pair<unsigned, unsigned> size() const {
        winsize dimensions = {};
        if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &dimensions) != 0) {
            return {80U, 24U};
        }
        return {
            dimensions.ws_col == 0U ? 80U : dimensions.ws_col,
            dimensions.ws_row == 0U ? 24U : dimensions.ws_row,
        };
    }

private:
    void closeOwnedInput() {
        if (ownsInputFD_ && inputFD_ >= 0) {
            close(inputFD_);
            inputFD_ = -1;
            ownsInputFD_ = false;
        }
    }

    termios original_ = {};
    int inputFD_ = -1;
    int originalFlags_ = -1;
    bool ownsInputFD_ = false;
    bool active_ = false;
    std::string error_;
};

class StressLoad {
public:
    StressLoad() {
        threads_.reserve(kMaximumWorkers);
        for (unsigned index = 0U; index < kMaximumWorkers; ++index) {
            threads_.emplace_back(&StressLoad::worker, this, index);
        }
    }

    ~StressLoad() {
        stopping_.store(true, std::memory_order_relaxed);
        for (std::thread &thread : threads_) {
            if (thread.joinable()) {
                thread.join();
            }
        }
    }

    void setDuty(int duty) {
        duty_.store(std::max(0, std::min(100, duty)), std::memory_order_relaxed);
    }

    void setWorkers(int workers) {
        activeWorkers_.store(
            std::max(1, std::min(static_cast<int>(kMaximumWorkers), workers)),
            std::memory_order_relaxed
        );
    }

    void setEnabled(bool enabled) {
        enabled_.store(enabled, std::memory_order_relaxed);
    }

    int duty() const {
        return duty_.load(std::memory_order_relaxed);
    }

    int workerCount() const {
        return activeWorkers_.load(std::memory_order_relaxed);
    }

    bool enabled() const {
        return enabled_.load(std::memory_order_relaxed);
    }

    bool running() const {
        return enabled() && duty() > 0;
    }

private:
    void worker(unsigned index) {
        using Clock = std::chrono::steady_clock;
        constexpr auto cycle = std::chrono::milliseconds(100);
        __m256d a = _mm256_set_pd(1.001, 1.002, 1.003, 1.004);
        __m256d b = _mm256_set_pd(0.999, 0.998, 0.997, 0.996);
        __m256d c = _mm256_set_pd(0.101, 0.202, 0.303, 0.404);
        __m256d d = _mm256_set_pd(0.505, 0.606, 0.707, 0.808);
        const __m256d decay = _mm256_set1_pd(0.999999999);

        while (!stopping_.load(std::memory_order_relaxed)) {
            const auto cycleStart = Clock::now();
            const int duty = duty_.load(std::memory_order_relaxed);
            const bool active = enabled_.load(std::memory_order_relaxed)
                && duty > 0
                && static_cast<int>(index) < activeWorkers_.load(std::memory_order_relaxed);
            const auto busyFor = std::chrono::microseconds(duty * 1000);
            const auto busyUntil = cycleStart + busyFor;
            while (active
                   && !stopping_.load(std::memory_order_relaxed)
                   && Clock::now() < busyUntil) {
                for (unsigned iteration = 0U; iteration < 4096U; ++iteration) {
                    a = _mm256_mul_pd(_mm256_fmadd_pd(a, b, c), decay);
                    c = _mm256_mul_pd(_mm256_fnmadd_pd(c, d, a), decay);
                    b = _mm256_mul_pd(_mm256_fmadd_pd(b, d, a), decay);
                    d = _mm256_mul_pd(_mm256_fnmadd_pd(d, a, c), decay);
                }
                alignas(32) double values[4];
                _mm256_store_pd(values, _mm256_add_pd(a, c));
                resultSinks[index] = values[0] + values[1] + values[2] + values[3];
            }
            std::this_thread::sleep_until(cycleStart + cycle);
        }
    }

    std::atomic<bool> stopping_{false};
    std::atomic<bool> enabled_{false};
    std::atomic<int> duty_{0};
    std::atomic<int> activeWorkers_{8};
    std::vector<std::thread> threads_;
};

class FrequencySampler {
public:
    explicit FrequencySampler(bool enabled) : enabled_(enabled) {
        if (enabled_) {
            thread_ = std::thread(&FrequencySampler::run, this);
        }
    }

    ~FrequencySampler() {
        stopping_.store(true, std::memory_order_relaxed);
        if (thread_.joinable()) {
            thread_.join();
        }
    }

    double mhz() const {
        return mhz_.load(std::memory_order_relaxed);
    }

    double percent() const {
        return percent_.load(std::memory_order_relaxed);
    }

    double ageSeconds() const {
        const int64_t update = updatedAtNS_.load(std::memory_order_relaxed);
        if (update == 0) {
            return std::numeric_limits<double>::infinity();
        }
        return static_cast<double>(monotonicNanoseconds() - update) / 1e9;
    }

private:
    void run() {
        constexpr const char *command =
            "/usr/bin/powermetrics -n 1 -i 250 -b 1 --samplers cpu_power </dev/null 2>/dev/null";
        while (!stopping_.load(std::memory_order_relaxed)) {
            FILE *stream = popen(command, "r");
            if (stream != nullptr) {
                char line[1024];
                while (!stopping_.load(std::memory_order_relaxed)
                       && std::fgets(line, sizeof(line), stream) != nullptr) {
                    double parsedMHz = 0.0;
                    double parsedPercent = 0.0;
                    if (turbomactop::parsePowermetricsFrequency(
                            line, &parsedMHz, &parsedPercent)) {
                        mhz_.store(parsedMHz, std::memory_order_relaxed);
                        percent_.store(parsedPercent, std::memory_order_relaxed);
                        updatedAtNS_.store(monotonicNanoseconds(), std::memory_order_relaxed);
                        break;
                    }
                }
                pclose(stream);
            }
            for (unsigned tenth = 0U;
                 tenth < 5U && !stopping_.load(std::memory_order_relaxed);
                 ++tenth) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        }
    }

    bool enabled_ = true;
    std::atomic<bool> stopping_{false};
    std::atomic<double> mhz_{0.0};
    std::atomic<double> percent_{0.0};
    std::atomic<int64_t> updatedAtNS_{0};
    std::thread thread_;
};

class CPUBusyMeter {
public:
    double sample() {
        host_cpu_load_info_data_t info = {};
        mach_msg_type_number_t count = HOST_CPU_LOAD_INFO_COUNT;
        const kern_return_t result = host_statistics(
            mach_host_self(),
            HOST_CPU_LOAD_INFO,
            reinterpret_cast<host_info_t>(&info),
            &count
        );
        if (result != KERN_SUCCESS) {
            return value_;
        }
        const uint64_t user = info.cpu_ticks[CPU_STATE_USER];
        const uint64_t nice = info.cpu_ticks[CPU_STATE_NICE];
        const uint64_t system = info.cpu_ticks[CPU_STATE_SYSTEM];
        const uint64_t idle = info.cpu_ticks[CPU_STATE_IDLE];
        const uint64_t total = user + nice + system + idle;
        if (havePrevious_ && total > previousTotal_) {
            const uint64_t idleDelta = idle - previousIdle_;
            const uint64_t totalDelta = total - previousTotal_;
            value_ = 100.0 * static_cast<double>(totalDelta - idleDelta)
                / static_cast<double>(totalDelta);
        }
        previousIdle_ = idle;
        previousTotal_ = total;
        havePrevious_ = true;
        return value_;
    }

private:
    bool havePrevious_ = false;
    uint64_t previousIdle_ = 0U;
    uint64_t previousTotal_ = 0U;
    double value_ = 0.0;
};

bool requestStatus(std::string *json, std::string *error) {
    const int descriptor = socket(AF_UNIX, SOCK_STREAM, 0);
    if (descriptor < 0) {
        *error = std::strerror(errno);
        return false;
    }
    const timeval timeout = {0, 500000};
    setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    sockaddr_un address = {};
    address.sun_family = AF_UNIX;
    std::snprintf(address.sun_path, sizeof(address.sun_path), "%s", kSocketPath);
    if (connect(descriptor, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0) {
        *error = std::strerror(errno);
        close(descriptor);
        return false;
    }
    constexpr const char payload[] = "status json\n";
    if (write(descriptor, payload, sizeof(payload) - 1U)
        != static_cast<ssize_t>(sizeof(payload) - 1U)) {
        *error = std::strerror(errno);
        close(descriptor);
        return false;
    }
    shutdown(descriptor, SHUT_WR);
    json->clear();
    std::array<char, 4096> buffer = {};
    while (json->size() < 131072U) {
        const ssize_t count = read(descriptor, buffer.data(), buffer.size());
        if (count == 0) {
            break;
        }
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            *error = std::strerror(errno);
            close(descriptor);
            return false;
        }
        json->append(buffer.data(), static_cast<std::size_t>(count));
    }
    close(descriptor);
    if (json->empty()) {
        *error = "empty daemon response";
        return false;
    }
    return true;
}

std::string powerTape(const turbomactop::Snapshot &status, unsigned width) {
    if (!status.valid || status.capacityW <= 0.0 || width < 10U) {
        return std::string(width, '-');
    }
    std::ostringstream output;
    const double scale = status.capacityW / static_cast<double>(width);
    const unsigned filled = static_cast<unsigned>(
        std::min<double>(width, std::max(0.0, status.inputW / scale))
    );
    const char *activeColor = nullptr;
    for (unsigned column = 0U; column < width; ++column) {
        const double watts = (static_cast<double>(column) + 0.5) * scale;
        const char *color = watts >= status.emergencyW ? kRed
            : watts >= status.shedW ? kOrange
            : watts >= status.guardW ? kAmber
            : kGreen;
        if (color != activeColor) {
            output << color;
            activeColor = color;
        }
        output << (column < filled ? "█" : "·");
    }
    output << kReset;
    return output.str();
}

struct Peaks {
    double inputW = 0.0;
    double packageW = 0.0;
    double tempC = 0.0;
    double frequencyMHz = 0.0;
};

void resetPeaks(Peaks *peaks, const turbomactop::Snapshot &status, double frequencyMHz) {
    peaks->inputW = status.valid ? status.inputW : 0.0;
    peaks->packageW = status.valid ? status.packageW : 0.0;
    peaks->tempC = status.valid ? status.cpuTempC : 0.0;
    peaks->frequencyMHz = std::max(0.0, frequencyMHz);
}

void commitFrame(std::ostringstream &output) {
    const std::string raw = output.str();
    std::string frame;
    frame.reserve(raw.size() + 32U);
    std::size_t position = 0U;
    for (;;) {
        const std::size_t newline = raw.find('\n', position);
        if (newline == std::string::npos) {
            break;
        }
        frame.append(raw, position, newline - position);
        frame.append(kReset);
        frame.append("\x1b[K\n");
        position = newline + 1U;
    }
    frame.append(raw, position, raw.size() - position);
    frame.append("\x1b[0J");
    std::cout << frame << std::flush;
}

void render(
    unsigned columns,
    unsigned rows,
    const turbomactop::Snapshot &status,
    double statusAge,
    double frequencyMHz,
    double frequencyPercent,
    double frequencyAge,
    double cpuBusy,
    const StressLoad &stress,
    const Peaks &peaks,
    const std::vector<double> &history,
    const std::deque<std::string> &events,
    const std::string &lastError,
    const turbomactop::WarningState &warnings,
    uint64_t receivedControls,
    const std::string &lastControl,
    double now
) {
    // In-place update on the alternate screen: the full clear happens once
    // when the session starts (TerminalSession), never per frame. Homing the
    // cursor and overwriting, with erase-to-end-of-line ahead of every
    // newline and one clear-to-end-of-screen after the final line, keeps a
    // shrinking frame from leaving stale rows without ever re-clearing
    // enough of the buffer to cause scroll artifacts.
    std::ostringstream output;
    output << "\x1b[H";
    if (columns < 88U || rows < 26U) {
        output << kBgRed << kBold
               << " TURBOMACTOP REQUIRES AT LEAST 88 x 26 " << kReset << "\n"
               << "Current terminal: " << columns << " x " << rows << "\n"
               << "Resize the terminal. q quits; load remains "
               << (stress.running() ? "ACTIVE" : "stopped") << ".\n";
        commitFrame(output);
        return;
    }

    const unsigned tapeWidth = std::min(72U, columns - 16U);
    const bool flash = static_cast<int>(now * 2.0) % 2 == 0;
    const char *master = warnings.level == turbomactop::WarningLevel::Warning
        ? (flash ? kBgRed : kRed)
        : warnings.level == turbomactop::WarningLevel::Caution
            ? kBgAmber
            : kBgGreen;
    const std::string masterText = warnings.level == turbomactop::WarningLevel::Warning
        ? " MASTER WARNING "
        : warnings.level == turbomactop::WarningLevel::Caution
            ? " MASTER CAUTION "
            : " SYSTEM NORMAL ";

    output << kCyan << kBold
           << "TURBOMAC POWER FLIGHT DECK  //  CPU PACKAGE TEST CONSOLE"
           << kReset << "\n";
    output << master << kBold << masterText << kReset << "  "
           << lamp("GOV ARMED", status.valid && status.armed) << " "
           << lamp("TELEM LIVE", status.valid && status.telemetryReady) << " "
           << lamp("HWP READBACK", status.valid && status.hwpReadbackValid) << " "
           << lamp("APPLE GUARD", status.valid && status.appleGuardEnabled, kBgAmber)
           << "\n";

    output << "STATE  "
           << lamp("CRUISE", status.band == "cruise") << " "
           << lamp("GUARD", status.band == "guard", kBgAmber) << " "
           << lamp("SHED", status.band == "shed", kBgRed) << " "
           << lamp("EMERGENCY", status.band == "emergency", kBgRed)
           << "  " << colorForBand(status.band) << kBold << upper(status.band) << kReset
           << "\n";

    output << kGray << std::string(std::min<unsigned>(columns, 118U), '=') << kReset << "\n";
    output << kCyan << kBold << "DC INPUT BUS" << kReset
           << "   TOTAL " << colorForBand(status.band) << kBold << fixed(status.inputW) << " W" << kReset
           << "   ADAPTER " << fixed(status.capacityW) << " W"
           << "   HEADROOM " << fixed(status.capacityW - status.inputW) << " W\n";
    output << "           " << powerTape(status, tapeWidth) << "\n";
    output << "           0 W"
           << "   G " << fixed(status.guardW)
           << "   S " << fixed(status.shedW)
           << "   E " << fixed(status.emergencyW)
           << "   CAP " << fixed(status.capacityW) << "\n";
    output << "TREND 60s  " << kCyan
           << turbomactop::sparkline(history, tapeWidth, std::max(1.0, status.capacityW))
           << kReset << "\n";

    output << kGray << std::string(std::min<unsigned>(columns, 118U), '-') << kReset << "\n";
    output << kCyan << kBold << "CPU ENGINE / RAPL" << kReset
           << "   FREQ " << kWhite << kBold << fixed(frequencyMHz, 0) << " MHz" << kReset
           << " (" << fixed(frequencyPercent, 0) << "% nominal)"
           << "   BUSY " << fixed(cpuBusy, 0) << "%"
           << "   TEMP " << (status.cpuTempC >= 95.0 ? kRed : status.cpuTempC >= 85.0 ? kAmber : kGreen)
           << kBold << fixed(status.cpuTempC) << " C" << kReset << "\n";
    output << "PACKAGE " << fixed(status.packageW) << " W"
           << "   OTHER EST " << fixed(status.nonCPUW) << " W"
           << "   PL1 " << kCyan << kBold << fixed(status.pl1W) << " W" << kReset
           << "   PL2 " << kCyan << kBold << fixed(status.pl2W) << " W" << kReset
           << "   HWP CPUs " << status.hwpLogicalCPUCount << "\n";

    const double scheduled = static_cast<double>(stress.duty() * stress.workerCount())
        / static_cast<double>(kMaximumWorkers);
    output << kGray << std::string(std::min<unsigned>(columns, 118U), '-') << kReset << "\n";
    output << kCyan << kBold << "MANUAL THRUST / AVX2-FMA" << kReset << "   "
           << (stress.running() ? kBgRed : kBgDark) << kBold
           << (stress.running() ? " LOAD ACTIVE " : " LOAD STOPPED ") << kReset
           << "   DUTY " << kWhite << kBold << stress.duty() << "%" << kReset
           << "   WORKERS " << stress.workerCount() << "/" << kMaximumWorkers
           << "   SCHEDULED " << fixed(scheduled, 0) << "% package\n";
    output << kRed << kBold
           << "NO AUTOMATIC CUTOFF — WARNINGS DO NOT REDUCE OR STOP THE LOAD"
           << kReset << "\n";
    output << "CONTROLS  ↑/↓ or W/S duty   ←/→ or A/D workers   SPACE/ENTER toggle   M max   0/X idle   Q quit\n";
    output << "KEYBOARD  RX " << receivedControls << "   LAST "
           << (receivedControls == 0U ? kAmber : kGreen) << kBold
           << lastControl << kReset << "\n";

    output << kGray << std::string(std::min<unsigned>(columns, 118U), '-') << kReset << "\n";
    output << kCyan << kBold << "ANNUNCIATOR PANEL" << kReset << "   ";
    const std::size_t warningCount = std::min<std::size_t>(warnings.messages.size(), 3U);
    for (std::size_t index = 0U; index < warningCount; ++index) {
        const char *color = warnings.level == turbomactop::WarningLevel::Warning
            ? kRed
            : warnings.level == turbomactop::WarningLevel::Caution ? kAmber : kGreen;
        output << color << "[" << warnings.messages[index] << "]" << kReset << " ";
    }
    output << "\n";
    output << "DATA AGE status " << fixed(statusAge, 1) << "s"
           << " / frequency " << fixed(frequencyAge, 1) << "s";
    if (!lastError.empty()) {
        output << "   " << kRed << lastError << kReset;
    }
    output << "\n";
    output << "SESSION PEAKS  input " << fixed(peaks.inputW) << " W"
           << "   package " << fixed(peaks.packageW) << " W"
           << "   temp " << fixed(peaks.tempC) << " C"
           << "   freq " << fixed(peaks.frequencyMHz, 0) << " MHz\n";

    output << kGray << std::string(std::min<unsigned>(columns, 118U), '-') << kReset << "\n";
    output << kCyan << kBold << "EVENT STRIP" << kReset << "   ";
    if (events.empty()) {
        output << kDim << "waiting for state transition" << kReset;
    } else {
        const std::size_t begin = events.size() > 3U ? events.size() - 3U : 0U;
        for (std::size_t index = begin; index < events.size(); ++index) {
            output << events[index];
            if (index + 1U < events.size()) {
                output << "  |  ";
            }
        }
    }
    output << "\n";

    commitFrame(output);
}

void usage(const char *program) {
    std::fprintf(stderr, "usage: %s [--no-frequency]\n", program);
}

} // namespace

int main(int argc, char **argv) {
    bool frequencyEnabled = true;
    for (int index = 1; index < argc; ++index) {
        if (std::strcmp(argv[index], "--no-frequency") == 0) {
            frequencyEnabled = false;
        } else if (std::strcmp(argv[index], "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 2;
        }
    }
    if (geteuid() != 0) {
        std::fputs("turbomactop must run as root to read the protected TurboMac socket and powermetrics\n",
                   stderr);
        return 1;
    }

    std::signal(SIGINT, handleSignal);
    std::signal(SIGTERM, handleSignal);
    std::signal(SIGHUP, handleSignal);

    TerminalSession terminal;
    if (!terminal.active()) {
        std::fprintf(stderr, "%s\n", terminal.error().c_str());
        return 1;
    }

    StressLoad stress;
    FrequencySampler frequency(frequencyEnabled);
    CPUBusyMeter cpuBusyMeter;
    turbomactop::Snapshot status;
    Peaks peaks;
    std::vector<double> history;
    history.reserve(kHistorySamples);
    std::deque<std::string> events;
    std::string lastBand;
    std::string lastError;
    double lastStatusAt = 0.0;
    double lastPollAt = 0.0;
    double lastBusyPollAt = 0.0;
    double cpuBusy = 0.0;
    turbomactop::WarningLevel previousWarning = turbomactop::WarningLevel::Normal;
    const double sessionStartedAt = monotonicSeconds();
    std::string pendingInput;
    double pendingInputAt = 0.0;
    uint64_t receivedControls = 0U;
    std::string lastControl = "WAITING FOR INPUT";

    while (!stopRequested) {
        const double now = monotonicSeconds();
        char input[128];
        const ssize_t count = read(terminal.inputFD(), input, sizeof(input));
        if (count > 0) {
            pendingInput.append(input, static_cast<std::size_t>(count));
            pendingInputAt = now;
        }
        while (!pendingInput.empty()) {
            turbomactop::ControlAction action = turbomactop::ControlAction::None;
            if (!turbomactop::consumeControlInput(&pendingInput, &action)) {
                break;
            }
            if (action == turbomactop::ControlAction::None) {
                continue;
            }
            receivedControls++;
            lastControl = turbomactop::controlActionName(action);
            switch (action) {
                case turbomactop::ControlAction::DutyUp:
                    stress.setDuty(stress.duty() + 5);
                    break;
                case turbomactop::ControlAction::DutyDown:
                    stress.setDuty(stress.duty() - 5);
                    break;
                case turbomactop::ControlAction::WorkersUp:
                    stress.setWorkers(stress.workerCount() + 1);
                    break;
                case turbomactop::ControlAction::WorkersDown:
                    stress.setWorkers(stress.workerCount() - 1);
                    break;
                case turbomactop::ControlAction::ToggleLoad:
                    if (!stress.enabled() && stress.duty() == 0) {
                        stress.setDuty(5);
                    }
                    stress.setEnabled(!stress.enabled());
                    break;
                case turbomactop::ControlAction::StopLoad:
                    stress.setEnabled(false);
                    stress.setDuty(0);
                    break;
                case turbomactop::ControlAction::MaximumPreset:
                    stress.setDuty(100);
                    stress.setWorkers(static_cast<int>(kMaximumWorkers));
                    break;
                case turbomactop::ControlAction::ResetPeaks:
                    resetPeaks(&peaks, status, frequency.mhz());
                    break;
                case turbomactop::ControlAction::Quit:
                    stopRequested = 1;
                    break;
                case turbomactop::ControlAction::None:
                    break;
            }
        }
        if (!pendingInput.empty() && now - pendingInputAt > 0.2) {
            pendingInput.erase(0U, 1U);
        }

        if (now - lastPollAt >= 0.25) {
            lastPollAt = now;
            std::string json;
            std::string requestError;
            turbomactop::Snapshot parsed;
            if (requestStatus(&json, &requestError)
                && turbomactop::parseStatusJSON(json, &parsed, &requestError)) {
                status = parsed;
                lastStatusAt = now;
                lastError.clear();
                if (lastBand.empty() || status.band != lastBand) {
                    std::ostringstream event;
                    event << "+" << fixed(now - sessionStartedAt, 0)
                          << "s " << upper(status.band);
                    events.push_back(event.str());
                    if (events.size() > 12U) {
                        events.pop_front();
                    }
                    lastBand = status.band;
                }
                if (history.size() == kHistorySamples) {
                    history.erase(history.begin());
                }
                history.push_back(status.inputW);
                peaks.inputW = std::max(peaks.inputW, status.inputW);
                peaks.packageW = std::max(peaks.packageW, status.packageW);
                peaks.tempC = std::max(peaks.tempC, status.cpuTempC);
            } else {
                lastError = "STATUS: " + requestError;
            }
        }
        if (now - lastBusyPollAt >= 0.5) {
            lastBusyPollAt = now;
            cpuBusy = cpuBusyMeter.sample();
        }

        const double statusAge = lastStatusAt == 0.0
            ? std::numeric_limits<double>::infinity()
            : now - lastStatusAt;
        const double frequencyAge = frequency.ageSeconds();
        const double frequencyMHz = frequency.mhz();
        peaks.frequencyMHz = std::max(peaks.frequencyMHz, frequencyMHz);
        const turbomactop::WarningState warnings = turbomactop::assessWarnings(
            status,
            statusAge > kStatusStaleSeconds,
            frequencyEnabled && frequencyAge > kFrequencyStaleSeconds,
            stress.running()
        );
        if (static_cast<int>(warnings.level) > static_cast<int>(previousWarning)) {
            std::cout << '\a';
        }
        previousWarning = warnings.level;
        const auto [columns, rows] = terminal.size();
        render(
            columns,
            rows,
            status,
            statusAge,
            frequencyMHz,
            frequency.percent(),
            frequencyAge,
            cpuBusy,
            stress,
            peaks,
            history,
            events,
            lastError,
            warnings,
            receivedControls,
            lastControl,
            now
        );
        std::this_thread::sleep_for(std::chrono::milliseconds(80));
    }

    stress.setEnabled(false);
    stress.setDuty(0);
    return 0;
}
