#ifndef TURBOMAC_PROFILE_H
#define TURBOMAC_PROFILE_H

#include <cstdint>
#include <string>

struct MachineIdentity {
    std::string hardwareUUID;
    std::string modelIdentifier;
    std::string cpuBrand;
    std::string osProductVersion;
    std::string osBuild;
    std::string pdtrType;
    std::string acpwType;
};

struct CalibrationProfile {
    uint32_t schemaVersion = 2U;
    MachineIdentity identity;
    double calibratedCapacityW = 0.0;
    double reserveW = 2.0;
    double raplFloorW = 0.0;
    double raplCeilingPL1W = 0.0;
    double raplCeilingPL2W = 0.0;
    double cruisePL2BurstW = 0.0;
    double packageToInputSlope = 1.0;
    double packageToInputInterceptW = 0.0;
    double guardRatio = 0.5661;
    double shedRatio = 0.6541;
    double emergencyRatio = 0.7296;
    double cruiseTargetRatio = 0.5661;
    double guardTargetRatio = 0.5221;
    double shedTargetRatio = 0.4781;
    uint32_t calibrationPointCount = 0U;
    bool performanceMode = false;
    double performancePL1W = 0.0;
    double performancePL2W = 0.0;
    bool validationPassed = false;
    bool autoArm = false;
    int64_t calibratedAt = 0;
    int64_t validatedAt = 0;
};

class ProfileStore {
public:
    static constexpr const char *kDirectory = "/Library/Application Support/TurboMac";
    static constexpr const char *kPath = "/Library/Application Support/TurboMac/calibration.conf";

    static bool currentIdentity(
        const std::string &pdtrType,
        const std::string &acpwType,
        MachineIdentity *identity,
        std::string *error
    );
    static bool load(CalibrationProfile *profile, std::string *error);
    static bool save(const CalibrationProfile &profile, std::string *error);
    static bool validate(const CalibrationProfile &profile, std::string *error);
    static bool matches(
        const CalibrationProfile &profile,
        const MachineIdentity &identity,
        std::string *error
    );
};

#endif
