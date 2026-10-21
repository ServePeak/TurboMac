#include "Profile.h"

#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOKitKeys.h>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <map>
#include <set>
#include <sstream>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>

namespace {

bool setError(std::string *error, const std::string &message) {
    if (error != nullptr) {
        *error = message;
    }
    return false;
}

bool sysctlString(const char *name, std::string *value, std::string *error) {
    size_t length = 0;
    if (sysctlbyname(name, nullptr, &length, nullptr, 0) != 0 || length < 1U) {
        return setError(error, std::string("sysctl ") + name + " failed");
    }
    std::string buffer(length, '\0');
    if (sysctlbyname(name, buffer.data(), &length, nullptr, 0) != 0) {
        return setError(error, std::string("sysctl ") + name + " failed");
    }
    if (!buffer.empty() && buffer.back() == '\0') {
        buffer.pop_back();
    }
    *value = buffer;
    return true;
}

bool platformUUID(std::string *value, std::string *error) {
    io_registry_entry_t root = IORegistryEntryFromPath(kIOMainPortDefault, "IOService:/");
    if (root == IO_OBJECT_NULL) {
        return setError(error, "IOPlatformExpert registry root was not found");
    }
    CFTypeRef property = IORegistryEntryCreateCFProperty(
        root,
        CFSTR(kIOPlatformUUIDKey),
        kCFAllocatorDefault,
        0
    );
    IOObjectRelease(root);
    if (property == nullptr || CFGetTypeID(property) != CFStringGetTypeID()) {
        if (property != nullptr) {
            CFRelease(property);
        }
        return setError(error, "IOPlatformUUID was not found");
    }
    char buffer[128] = {};
    const bool converted = CFStringGetCString(
        static_cast<CFStringRef>(property),
        buffer,
        sizeof(buffer),
        kCFStringEncodingUTF8
    );
    CFRelease(property);
    if (!converted) {
        return setError(error, "IOPlatformUUID could not be decoded");
    }
    *value = buffer;
    return true;
}

bool safeText(const std::string &value) {
    if (value.empty() || value.size() > 256U) {
        return false;
    }
    for (unsigned char character : value) {
        if (character < 0x20U || character == 0x7fU || character == '=') {
            return false;
        }
    }
    return true;
}

bool parseDouble(const std::string &input, double *value) {
    errno = 0;
    char *end = nullptr;
    const double parsed = std::strtod(input.c_str(), &end);
    if (errno != 0 || end == input.c_str() || *end != '\0' || !std::isfinite(parsed)) {
        return false;
    }
    *value = parsed;
    return true;
}

bool parseInt64(const std::string &input, int64_t *value) {
    errno = 0;
    char *end = nullptr;
    const long long parsed = std::strtoll(input.c_str(), &end, 10);
    if (errno != 0 || end == input.c_str() || *end != '\0') {
        return false;
    }
    *value = (int64_t)parsed;
    return true;
}

bool checkProtectedPath(std::string *error) {
    struct stat directory = {};
    struct stat file = {};
    if (lstat(ProfileStore::kDirectory, &directory) != 0
        || !S_ISDIR(directory.st_mode)
        || directory.st_uid != 0U
        || (directory.st_mode & 0022U) != 0U) {
        return setError(error, "profile directory must be root-owned and not group/world-writable");
    }
    if (lstat(ProfileStore::kPath, &file) != 0
        || !S_ISREG(file.st_mode)
        || file.st_uid != 0U
        || (file.st_mode & 0077U) != 0U) {
        return setError(error, "profile must be a root-owned regular file with mode 0600");
    }
    return true;
}

std::string line(const char *key, const std::string &value) {
    return std::string(key) + "=" + value + "\n";
}

std::string line(const char *key, double value) {
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%.6f", value);
    return line(key, buffer);
}

} // namespace

bool ProfileStore::currentIdentity(
    const std::string &pdtrType,
    const std::string &acpwType,
    MachineIdentity *identity,
    std::string *error
) {
    if (identity == nullptr || !safeText(pdtrType) || !safeText(acpwType)) {
        return setError(error, "invalid SMC identity");
    }
    if (!platformUUID(&identity->hardwareUUID, error)
        || !sysctlString("hw.model", &identity->modelIdentifier, error)
        || !sysctlString("machdep.cpu.brand_string", &identity->cpuBrand, error)
        || !sysctlString("kern.osproductversion", &identity->osProductVersion, error)
        || !sysctlString("kern.osversion", &identity->osBuild, error)) {
        return false;
    }
    identity->pdtrType = pdtrType;
    identity->acpwType = acpwType;
    return true;
}

bool ProfileStore::load(CalibrationProfile *profile, std::string *error) {
    if (profile == nullptr || !checkProtectedPath(error)) {
        return false;
    }
    const int descriptor = ::open(kPath, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) {
        return setError(error, "profile could not be opened");
    }
    std::string content;
    char buffer[4096];
    for (;;) {
        const ssize_t count = ::read(descriptor, buffer, sizeof(buffer));
        if (count < 0) {
            ::close(descriptor);
            return setError(error, "profile read failed");
        }
        if (count == 0) {
            break;
        }
        content.append(buffer, (size_t)count);
        if (content.size() > 16384U) {
            ::close(descriptor);
            return setError(error, "profile is too large");
        }
    }
    ::close(descriptor);

    std::map<std::string, std::string> values;
    std::istringstream stream(content);
    std::string current;
    while (std::getline(stream, current)) {
        if (current.empty() || current[0] == '#') {
            continue;
        }
        const size_t separator = current.find('=');
        if (separator == std::string::npos || separator == 0U) {
            return setError(error, "profile contains a malformed line");
        }
        const std::string key = current.substr(0U, separator);
        const std::string value = current.substr(separator + 1U);
        if (!safeText(key) || !safeText(value) || !values.emplace(key, value).second) {
            return setError(error, "profile contains an invalid or duplicate key");
        }
    }

    const std::set<std::string> common = {
        "schema_version", "hardware_uuid", "model_identifier", "cpu_brand",
        "os_product_version", "os_build", "smc_pdtr_type", "smc_acpw_type",
        "calibrated_capacity_w", "reserve_w", "rapl_floor_w",
        "rapl_ceiling_pl1_w", "rapl_ceiling_pl2_w", "cruise_pl2_burst_w",
        "package_to_input_slope", "package_to_input_intercept_w",
        "calibration_point_count", "validation_passed", "auto_arm",
        "calibrated_at", "validated_at"
    };
    const auto schemaValue = values.find("schema_version");
    int64_t schema = 0;
    if (schemaValue == values.end()
        || !parseInt64(schemaValue->second, &schema)
        || (schema != 1 && schema != 2 && schema != 3)) {
        return setError(error, "profile contains an unsupported schema version");
    }
    std::set<std::string> expected = common;
    if (schema == 2) {
        expected.insert({
            "guard_ratio", "shed_ratio", "emergency_ratio",
            "cruise_target_ratio", "guard_target_ratio", "shed_target_ratio"
        });
    }
    if (schema == 3) {
        expected.insert({
            "performance_mode", "performance_pl1_w", "performance_pl2_w"
        });
    }
    if (values.size() != expected.size()) {
        return setError(error, "profile has missing or unexpected keys");
    }
    for (const auto &item : values) {
        if (expected.count(item.first) == 0U) {
            return setError(error, "profile has an unexpected key: " + item.first);
        }
    }

    *profile = CalibrationProfile{};
    if (!parseDouble(values["calibrated_capacity_w"], &profile->calibratedCapacityW)
        || !parseDouble(values["reserve_w"], &profile->reserveW)
        || !parseDouble(values["rapl_floor_w"], &profile->raplFloorW)
        || !parseDouble(values["rapl_ceiling_pl1_w"], &profile->raplCeilingPL1W)
        || !parseDouble(values["rapl_ceiling_pl2_w"], &profile->raplCeilingPL2W)
        || !parseDouble(values["cruise_pl2_burst_w"], &profile->cruisePL2BurstW)
        || !parseDouble(values["package_to_input_slope"], &profile->packageToInputSlope)
        || !parseDouble(values["package_to_input_intercept_w"], &profile->packageToInputInterceptW)
        || !parseInt64(values["calibrated_at"], &profile->calibratedAt)
        || !parseInt64(values["validated_at"], &profile->validatedAt)
        || (values["validation_passed"] != "true" && values["validation_passed"] != "false")
        || (values["auto_arm"] != "true" && values["auto_arm"] != "false")) {
        return setError(error, "profile contains an invalid value");
    }
    if (schema == 2
        && (!parseDouble(values["guard_ratio"], &profile->guardRatio)
            || !parseDouble(values["shed_ratio"], &profile->shedRatio)
            || !parseDouble(values["emergency_ratio"], &profile->emergencyRatio)
            || !parseDouble(values["cruise_target_ratio"], &profile->cruiseTargetRatio)
            || !parseDouble(values["guard_target_ratio"], &profile->guardTargetRatio)
            || !parseDouble(values["shed_target_ratio"], &profile->shedTargetRatio))) {
        return setError(error, "profile contains an invalid control policy");
    }
    if (schema == 3
        && ((values["performance_mode"] != "true" && values["performance_mode"] != "false")
            || !parseDouble(values["performance_pl1_w"], &profile->performancePL1W)
            || !parseDouble(values["performance_pl2_w"], &profile->performancePL2W))) {
        return setError(error, "profile contains an invalid performance policy");
    }
    int64_t pointCount = 0;
    if (!parseInt64(values["calibration_point_count"], &pointCount)
        || pointCount < 1
        || pointCount > UINT32_MAX) {
        return setError(error, "profile contains an invalid calibration point count");
    }
    profile->calibrationPointCount = (uint32_t)pointCount;
    profile->schemaVersion = (uint32_t)schema;
    profile->performanceMode = schema == 3
        ? values["performance_mode"] == "true"
        : false;
    profile->validationPassed = values["validation_passed"] == "true";
    profile->autoArm = values["auto_arm"] == "true";
    profile->identity.hardwareUUID = values["hardware_uuid"];
    profile->identity.modelIdentifier = values["model_identifier"];
    profile->identity.cpuBrand = values["cpu_brand"];
    profile->identity.osProductVersion = values["os_product_version"];
    profile->identity.osBuild = values["os_build"];
    profile->identity.pdtrType = values["smc_pdtr_type"];
    profile->identity.acpwType = values["smc_acpw_type"];
    return validate(*profile, error);
}

bool ProfileStore::save(const CalibrationProfile &profile, std::string *error) {
    if (!validate(profile, error)) {
        return false;
    }
    if (::mkdir(kDirectory, 0755) != 0 && errno != EEXIST) {
        return setError(error, "profile directory could not be created");
    }
    if (::chown(kDirectory, 0, 0) != 0 || ::chmod(kDirectory, 0755) != 0) {
        return setError(error, "profile directory permissions could not be secured");
    }

    std::string content = "# TurboMac RAPL calibration profile v"
        + std::to_string(profile.schemaVersion) + "\n";
    content += line("schema_version", std::to_string(profile.schemaVersion));
    content += line("hardware_uuid", profile.identity.hardwareUUID);
    content += line("model_identifier", profile.identity.modelIdentifier);
    content += line("cpu_brand", profile.identity.cpuBrand);
    content += line("os_product_version", profile.identity.osProductVersion);
    content += line("os_build", profile.identity.osBuild);
    content += line("smc_pdtr_type", profile.identity.pdtrType);
    content += line("smc_acpw_type", profile.identity.acpwType);
    content += line("calibrated_capacity_w", profile.calibratedCapacityW);
    content += line("reserve_w", profile.reserveW);
    content += line("rapl_floor_w", profile.raplFloorW);
    content += line("rapl_ceiling_pl1_w", profile.raplCeilingPL1W);
    content += line("rapl_ceiling_pl2_w", profile.raplCeilingPL2W);
    content += line("cruise_pl2_burst_w", profile.cruisePL2BurstW);
    content += line("package_to_input_slope", profile.packageToInputSlope);
    content += line("package_to_input_intercept_w", profile.packageToInputInterceptW);
    if (profile.schemaVersion == 2U) {
        content += line("guard_ratio", profile.guardRatio);
        content += line("shed_ratio", profile.shedRatio);
        content += line("emergency_ratio", profile.emergencyRatio);
        content += line("cruise_target_ratio", profile.cruiseTargetRatio);
        content += line("guard_target_ratio", profile.guardTargetRatio);
        content += line("shed_target_ratio", profile.shedTargetRatio);
    }
    if (profile.schemaVersion == 3U) {
        content += line("performance_mode", profile.performanceMode ? "true" : "false");
        content += line("performance_pl1_w", profile.performancePL1W);
        content += line("performance_pl2_w", profile.performancePL2W);
    }
    content += line("calibration_point_count", std::to_string(profile.calibrationPointCount));
    content += line("validation_passed", profile.validationPassed ? "true" : "false");
    content += line("auto_arm", profile.autoArm ? "true" : "false");
    content += line("calibrated_at", std::to_string(profile.calibratedAt));
    content += line("validated_at", std::to_string(profile.validatedAt));

    char temporary[] = "/Library/Application Support/TurboMac/.calibration.conf.XXXXXX";
    const int descriptor = ::mkstemp(temporary);
    if (descriptor < 0) {
        return setError(error, "temporary profile could not be created");
    }
    fcntl(descriptor, F_SETFD, FD_CLOEXEC);
    bool ok = ::fchown(descriptor, 0, 0) == 0 && ::fchmod(descriptor, 0600) == 0;
    size_t written = 0U;
    while (ok && written < content.size()) {
        const ssize_t count = ::write(
            descriptor, content.data() + written, content.size() - written
        );
        if (count <= 0) {
            ok = false;
        } else {
            written += (size_t)count;
        }
    }
    ok = ok && ::fsync(descriptor) == 0;
    ::close(descriptor);
    if (!ok || ::rename(temporary, kPath) != 0) {
        ::unlink(temporary);
        return setError(error, "profile could not be committed atomically");
    }
    const int directoryFD = ::open(kDirectory, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directoryFD >= 0) {
        ::fsync(directoryFD);
        ::close(directoryFD);
    }
    return true;
}

bool ProfileStore::validate(const CalibrationProfile &profile, std::string *error) {
    const MachineIdentity &identity = profile.identity;
    if ((profile.schemaVersion != 1U && profile.schemaVersion != 2U && profile.schemaVersion != 3U)
        || !safeText(identity.hardwareUUID)
        || !safeText(identity.modelIdentifier)
        || !safeText(identity.cpuBrand)
        || !safeText(identity.osProductVersion)
        || !safeText(identity.osBuild)
        || !safeText(identity.pdtrType)
        || !safeText(identity.acpwType)
        || !std::isfinite(profile.calibratedCapacityW)
        || profile.calibratedCapacityW < 10.0
        || profile.calibratedCapacityW > 250.0
        || !std::isfinite(profile.reserveW)
        || profile.reserveW < 2.0
        || profile.reserveW > 20.0
        || !std::isfinite(profile.raplFloorW)
        || profile.raplFloorW < 1.0
        || !std::isfinite(profile.raplCeilingPL1W)
        || profile.raplCeilingPL1W < profile.raplFloorW
        || !std::isfinite(profile.raplCeilingPL2W)
        || profile.raplCeilingPL2W < profile.raplCeilingPL1W
        || !std::isfinite(profile.cruisePL2BurstW)
        || profile.cruisePL2BurstW < 0.0
        || profile.cruisePL2BurstW > 20.0
        || !std::isfinite(profile.packageToInputSlope)
        || profile.packageToInputSlope < 0.25
        || profile.packageToInputSlope > 4.0
        || !std::isfinite(profile.packageToInputInterceptW)
        || profile.packageToInputInterceptW < -20.0
        || profile.packageToInputInterceptW > 100.0
        || !std::isfinite(profile.guardRatio)
        || !std::isfinite(profile.shedRatio)
        || !std::isfinite(profile.emergencyRatio)
        || profile.guardRatio < 0.1
        || profile.guardRatio >= profile.shedRatio
        || profile.shedRatio >= profile.emergencyRatio
        || profile.emergencyRatio > 0.95
        || !std::isfinite(profile.cruiseTargetRatio)
        || !std::isfinite(profile.guardTargetRatio)
        || !std::isfinite(profile.shedTargetRatio)
        || profile.shedTargetRatio < 0.05
        || profile.shedTargetRatio >= profile.guardTargetRatio
        || profile.guardTargetRatio >= profile.cruiseTargetRatio
        || profile.cruiseTargetRatio > profile.guardRatio
        || profile.calibrationPointCount < 1U
        || profile.calibratedAt <= 0
        || (profile.validationPassed && profile.validatedAt < profile.calibratedAt)
        || (!profile.validationPassed && profile.validatedAt != 0)
        || (profile.autoArm && !profile.validationPassed)) {
        return setError(error, "calibration profile failed bounds validation");
    }
    // Schema 3 = performance profile: fixed RAPL targets instead of ACPW
    // bands. Targets must sit inside the profile ceilings, PL2 >= PL1, and
    // auto-arm is always refused: a performance hand-off is supervised only.
    if (profile.performanceMode != (profile.schemaVersion == 3U)
        || (profile.schemaVersion == 3U
            && (!std::isfinite(profile.performancePL1W)
                || profile.performancePL1W < profile.raplFloorW
                || profile.performancePL1W > profile.raplCeilingPL1W
                || !std::isfinite(profile.performancePL2W)
                || profile.performancePL2W < profile.performancePL1W
                || profile.performancePL2W > profile.raplCeilingPL2W
                || profile.autoArm))) {
        return setError(
            error,
            "performance profile failed bounds validation"
        );
    }
    return true;
}

bool ProfileStore::matches(
    const CalibrationProfile &profile,
    const MachineIdentity &identity,
    std::string *error
) {
    if (profile.identity.hardwareUUID != identity.hardwareUUID
        || profile.identity.modelIdentifier != identity.modelIdentifier
        || profile.identity.cpuBrand != identity.cpuBrand
        || profile.identity.osProductVersion != identity.osProductVersion
        || profile.identity.osBuild != identity.osBuild
        || profile.identity.pdtrType != identity.pdtrType
        || profile.identity.acpwType != identity.acpwType) {
        return setError(error, "calibration profile does not match CPU, SMC, machine, and OS identity");
    }
    return true;
}
