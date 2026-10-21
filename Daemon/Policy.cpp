#include "Policy.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace {

constexpr double kUnset = -1.0;
constexpr double kInputWindowSeconds = 1.0;
constexpr double kPackageWindowSeconds = 1.0;
constexpr double kPackageAlignmentWindowSeconds = 2.0;
constexpr double kCapacityWindowSeconds = 10.0;
constexpr double kTelemetryFreshSeconds = 2.0;
constexpr double kNonCPURiseTauSeconds = 0.25;
constexpr double kNonCPUFallTauSeconds = 5.0;
constexpr double kPackageAlignmentDeltaW = 5.0;
constexpr double kLimitDeadbandW = 0.25;
constexpr double kFastRecoveryIntervalSeconds = 1.0;
constexpr double kModerateRecoveryIntervalSeconds = 2.0;
constexpr double kConservativeRecoveryIntervalSeconds = 5.0;

double clampValue(double value, double low, double high) {
    return std::max(low, std::min(value, high));
}

} // namespace

GovernorPolicy::GovernorPolicy(const PolicyConfig &config) {
    reset(config);
}

void GovernorPolicy::reset(const PolicyConfig &config) {
    config_ = config;
    snapshot_ = {};
    inputSamples_.clear();
    packageSamples_.clear();
    capacitySamples_.clear();
    lastSMCTime_ = kUnset;
    lastPackageTime_ = kUnset;
    lastNonCPUTime_ = kUnset;
    lastNonCPUSMCTime_ = kUnset;
    guardHighSince_ = kUnset;
    shedHighSince_ = kUnset;
    belowShedSince_ = kUnset;
    belowGuardSince_ = kUnset;
    belowCruiseSince_ = kUnset;
    lastIncreaseTime_ = kUnset;
    batteryPercent_ = -1.0;
    nonCPUInitialized_ = false;
    limitInitialized_ = false;
}

bool GovernorPolicy::updateSMC(double nowSeconds, double inputW, double capacityW) {
    if (!std::isfinite(nowSeconds)
        || nowSeconds < 0.0
        || (lastSMCTime_ != kUnset && nowSeconds < lastSMCTime_)
        || !validPower(inputW, 250.0)
        || !validPower(capacityW, 250.0)
        || capacityW < 10.0) {
        return false;
    }
    inputSamples_.push_back({nowSeconds, inputW});
    capacitySamples_.push_back({nowSeconds, capacityW});
    lastSMCTime_ = nowSeconds;
    snapshot_.inputW = inputW;
    expireSamples(nowSeconds);
    updateThresholds();
    return true;
}

void GovernorPolicy::updateBatteryState(double percent) {
    // An unknown state of charge must fail safe: buffering only actuates
    // while a plausible percentage is available.
    if (!std::isfinite(percent) || percent < 0.0 || percent > 100.0) {
        batteryPercent_ = -1.0;
        return;
    }
    batteryPercent_ = percent;
}

bool GovernorPolicy::updatePackagePower(double nowSeconds, double packageW) {
    if (!std::isfinite(nowSeconds)
        || nowSeconds < 0.0
        || (lastPackageTime_ != kUnset && nowSeconds < lastPackageTime_)
        || !validPower(packageW, 200.0)) {
        return false;
    }
    lastPackageTime_ = nowSeconds;
    snapshot_.packageW = packageW;
    packageSamples_.push_back({nowSeconds, packageW});
    return true;
}

PolicySnapshot GovernorPolicy::evaluate(double nowSeconds) {
    if (!std::isfinite(nowSeconds) || nowSeconds < 0.0) {
        snapshot_.ready = false;
        return snapshot_;
    }
    expireSamples(nowSeconds);
    updateThresholds();
    snapshot_.batteryPercent = batteryPercent_;
    snapshot_.batteryBuffered = batteryPercent_ > kBatteryBufferSOCPercent;
    snapshot_.filteredInputW = filteredInput();
    snapshot_.filteredPackageW = filteredPackage(nowSeconds);
    snapshot_.alignedPackageW = alignedPackage(nowSeconds);
    snapshot_.ready = !inputSamples_.empty()
        && !packageSamples_.empty()
        && !capacitySamples_.empty()
        && sampleFresh(nowSeconds, lastSMCTime_, kTelemetryFreshSeconds)
        && sampleFresh(nowSeconds, lastPackageTime_, kTelemetryFreshSeconds)
        && snapshot_.capacityW > 0.0;
    if (!snapshot_.ready) {
        return snapshot_;
    }

    updateNonCPU(nowSeconds);
    // The rail estimate combines current filtered package power with the
    // edge-safe non-CPU estimate. The fast path deliberately does not reuse
    // that slow estimate: an instantaneous package sample plus a stale
    // subtraction was the source of false emergency transitions.
    const double railEstimate = snapshot_.filteredPackageW + snapshot_.nonCPUW;
    const double calibratedEstimate = config_.packageToInputSlope
        * snapshot_.packageW + config_.packageToInputInterceptW;
    snapshot_.predictedInputW = std::max(railEstimate, calibratedEstimate)
        + config_.reserveW;
    updateBand(nowSeconds);
    updateLimits(nowSeconds);
    snapshot_.nonCPUOverBudget = snapshot_.band == GovernorBand::Emergency
        && snapshot_.pl1W <= config_.raplFloorW + 0.05
        && (snapshot_.inputW >= snapshot_.emergencyW
            || snapshot_.predictedInputW >= snapshot_.emergencyW);
    return snapshot_;
}

const PolicySnapshot &GovernorPolicy::snapshot() const {
    return snapshot_;
}

const char *GovernorPolicy::bandName(GovernorBand band) {
    switch (band) {
        case GovernorBand::Cruise:
            return "cruise";
        case GovernorBand::Guard:
            return "guard";
        case GovernorBand::Shed:
            return "shed";
        case GovernorBand::Emergency:
            return "emergency";
    }
    return "unknown";
}

bool GovernorPolicy::validPower(double value, double maximum) {
    return std::isfinite(value) && value >= 0.0 && value <= maximum;
}

double GovernorPolicy::median(std::deque<TimedValue> values) {
    if (values.empty()) {
        return 0.0;
    }
    std::vector<double> sorted;
    sorted.reserve(values.size());
    for (const TimedValue &sample : values) {
        sorted.push_back(sample.value);
    }
    std::sort(sorted.begin(), sorted.end());
    const size_t middle = sorted.size() / 2U;
    return sorted.size() % 2U == 0U
        ? (sorted[middle - 1U] + sorted[middle]) / 2.0
        : sorted[middle];
}

double GovernorPolicy::elapsedOrZero(double now, double start) {
    return start == kUnset || now < start ? 0.0 : now - start;
}

void GovernorPolicy::updateCondition(bool condition, double now, double *since) {
    if (!condition) {
        *since = kUnset;
    } else if (*since == kUnset) {
        *since = now;
    }
}

void GovernorPolicy::expireSamples(double now) {
    while (!inputSamples_.empty()
           && now - inputSamples_.front().time > kInputWindowSeconds) {
        inputSamples_.pop_front();
    }
    while (!packageSamples_.empty()
           && now - packageSamples_.front().time > kPackageAlignmentWindowSeconds) {
        packageSamples_.pop_front();
    }
    while (!capacitySamples_.empty()
           && now - capacitySamples_.front().time > kCapacityWindowSeconds) {
        capacitySamples_.pop_front();
    }
}

void GovernorPolicy::updateThresholds() {
    snapshot_.capacityW = median(capacitySamples_);
    snapshot_.guardW = config_.guardRatio * snapshot_.capacityW;
    snapshot_.shedW = config_.shedRatio * snapshot_.capacityW;
    snapshot_.emergencyW = config_.emergencyRatio * snapshot_.capacityW;
}

void GovernorPolicy::updateNonCPU(double now) {
    // Update exactly once per SMC sample. evaluate() runs much faster than SMC;
    // repeatedly filtering the same phase-skewed pair artificially amplified
    // a single mismatch into tens of watts of supposed non-CPU consumption.
    if (lastSMCTime_ == lastNonCPUSMCTime_) {
        return;
    }
    // SMC input telemetry can trail the package energy counter at CPU load
    // edges. Normally retain the current median-package subtraction. When the
    // recent causal package envelope differs substantially from current
    // package power, block only an impossible upward attribution. Downward
    // updates remain available so the estimate can recover after a real load.
    // Direct measured input still owns the immediate emergency path.
    const double rawObservation = std::max(
        0.0,
        snapshot_.filteredInputW - snapshot_.filteredPackageW
    );
    double observation = rawObservation;
    if (nonCPUInitialized_
        && rawObservation > snapshot_.nonCPUW
        && snapshot_.alignedPackageW - snapshot_.filteredPackageW
            >= kPackageAlignmentDeltaW) {
        observation = snapshot_.nonCPUW;
    }
    snapshot_.rawNonCPUObservationW = rawObservation;
    snapshot_.nonCPUObservationW = observation;
    if (!nonCPUInitialized_) {
        snapshot_.nonCPUW = observation;
        nonCPUInitialized_ = true;
        lastNonCPUTime_ = now;
        lastNonCPUSMCTime_ = lastSMCTime_;
        return;
    }
    const double elapsed = clampValue(now - lastNonCPUTime_, 0.0, 2.0);
    const double tau = observation > snapshot_.nonCPUW
        ? kNonCPURiseTauSeconds
        : kNonCPUFallTauSeconds;
    const double alpha = elapsed <= 0.0 ? 0.0 : 1.0 - std::exp(-elapsed / tau);
    snapshot_.nonCPUW += alpha * (observation - snapshot_.nonCPUW);
    snapshot_.nonCPUW = std::max(0.0, snapshot_.nonCPUW);
    lastNonCPUTime_ = now;
    lastNonCPUSMCTime_ = lastSMCTime_;
}

void GovernorPolicy::updateBand(double now) {
    const bool emergency = snapshot_.inputW >= snapshot_.emergencyW
        || snapshot_.predictedInputW >= snapshot_.emergencyW;
    if (emergency) {
        snapshot_.band = GovernorBand::Emergency;
        belowShedSince_ = kUnset;
        belowGuardSince_ = kUnset;
        belowCruiseSince_ = kUnset;
        return;
    }

    updateCondition(snapshot_.filteredInputW >= snapshot_.guardW, now, &guardHighSince_);
    updateCondition(snapshot_.filteredInputW >= snapshot_.shedW, now, &shedHighSince_);

    switch (snapshot_.band) {
        case GovernorBand::Cruise:
            if (elapsedOrZero(now, shedHighSince_) >= 1.0) {
                snapshot_.band = GovernorBand::Shed;
            } else if (elapsedOrZero(now, guardHighSince_) >= 2.0) {
                snapshot_.band = GovernorBand::Guard;
            }
            break;
        case GovernorBand::Guard:
            if (elapsedOrZero(now, shedHighSince_) >= 1.0) {
                snapshot_.band = GovernorBand::Shed;
                belowCruiseSince_ = kUnset;
            } else {
                const double cruiseRecovery = snapshot_.guardW
                    - 0.03 * snapshot_.capacityW;
                updateCondition(snapshot_.filteredInputW < cruiseRecovery, now, &belowCruiseSince_);
                if (elapsedOrZero(now, belowCruiseSince_) >= 15.0) {
                    snapshot_.band = GovernorBand::Cruise;
                    belowCruiseSince_ = kUnset;
                }
            }
            break;
        case GovernorBand::Shed:
            updateCondition(snapshot_.filteredInputW < snapshot_.guardW, now, &belowGuardSince_);
            if (elapsedOrZero(now, belowGuardSince_) >= 10.0) {
                snapshot_.band = GovernorBand::Guard;
                belowGuardSince_ = kUnset;
            }
            break;
        case GovernorBand::Emergency:
            updateCondition(snapshot_.filteredInputW < snapshot_.shedW, now, &belowShedSince_);
            if (elapsedOrZero(now, belowShedSince_) >= 5.0) {
                snapshot_.band = GovernorBand::Shed;
                belowShedSince_ = kUnset;
            }
            break;
    }
}

void GovernorPolicy::updateLimits(double now) {
    // Battery-buffered allowance (operator request). While the battery is
    // above the 50% state-of-charge gate, it covers adapter deficits, so the
    // CPU limit tracks the cruise target without subtracting the non-CPU
    // estimate: total demand may exceed the DC input rail. Bands stay
    // computed as honest telemetry, live PL stays inside the verified
    // ceilings, and any unknown/invalid battery reading disables the mode.
    if (snapshot_.batteryBuffered) {
        snapshot_.stateTargetW = clampValue(
            config_.cruiseTargetRatio * snapshot_.capacityW - config_.reserveW,
            config_.raplFloorW,
            config_.raplCeilingPL1W
        );
        snapshot_.desiredPL1W = snapshot_.stateTargetW;
        snapshot_.pl1W = snapshot_.stateTargetW;
        snapshot_.pl2W = clampValue(
            snapshot_.pl1W + std::max(0.0, config_.cruisePL2BurstW),
            snapshot_.pl1W,
            config_.raplCeilingPL2W
        );
        snapshot_.recoveryIntervalS = 0.0;
        limitInitialized_ = true;
        lastIncreaseTime_ = now;
        return;
    }

    snapshot_.stateTargetW = targetForBand();
    double desiredPL1 = snapshot_.stateTargetW - snapshot_.nonCPUW - config_.reserveW;
    desiredPL1 = clampValue(desiredPL1, config_.raplFloorW, config_.raplCeilingPL1W);
    snapshot_.desiredPL1W = desiredPL1;
    snapshot_.recoveryIntervalS = recoveryInterval();

    if (!limitInitialized_) {
        snapshot_.pl1W = desiredPL1;
        limitInitialized_ = true;
        lastIncreaseTime_ = now;
    } else if (desiredPL1 < snapshot_.pl1W - kLimitDeadbandW) {
        snapshot_.pl1W = desiredPL1;
        lastIncreaseTime_ = now;
    } else if (desiredPL1 > snapshot_.pl1W + kLimitDeadbandW
               && elapsedOrZero(now, lastIncreaseTime_)
                    >= snapshot_.recoveryIntervalS) {
        snapshot_.pl1W = std::min(desiredPL1, snapshot_.pl1W + 1.0);
        lastIncreaseTime_ = now;
    }

    const double desiredPL2 = snapshot_.band == GovernorBand::Cruise
        ? snapshot_.pl1W + std::max(0.0, config_.cruisePL2BurstW)
        : snapshot_.pl1W;
    snapshot_.pl2W = clampValue(
        desiredPL2,
        snapshot_.pl1W,
        config_.raplCeilingPL2W
    );
}

bool GovernorPolicy::sampleFresh(
    double now,
    double sampleTime,
    double maximumAge
) const {
    return sampleTime != kUnset && now >= sampleTime && now - sampleTime <= maximumAge;
}

double GovernorPolicy::filteredInput() const {
    if (inputSamples_.empty()) {
        return 0.0;
    }
    double total = 0.0;
    for (const TimedValue &sample : inputSamples_) {
        total += sample.value;
    }
    return total / (double)inputSamples_.size();
}

double GovernorPolicy::filteredPackage(double now) const {
    if (packageSamples_.empty()) {
        return 0.0;
    }
    std::vector<double> values;
    values.reserve(packageSamples_.size());
    for (const TimedValue &sample : packageSamples_) {
        if (now >= sample.time && now - sample.time <= kPackageWindowSeconds) {
            values.push_back(sample.value);
        }
    }
    if (values.empty()) {
        return 0.0;
    }
    std::sort(values.begin(), values.end());
    const size_t middle = values.size() / 2U;
    return values.size() % 2U == 0U
        ? (values[middle - 1U] + values[middle]) / 2.0
        : values[middle];
}

double GovernorPolicy::alignedPackage(double now) const {
    double peak = snapshot_.filteredPackageW;
    for (const TimedValue &sample : packageSamples_) {
        if (now >= sample.time
            && now - sample.time <= kPackageAlignmentWindowSeconds) {
            peak = std::max(peak, sample.value);
        }
    }
    return peak;
}

double GovernorPolicy::recoveryInterval() const {
    if (snapshot_.capacityW <= 0.0) {
        return kConservativeRecoveryIntervalSeconds;
    }
    const double riskInput = std::max(snapshot_.inputW, snapshot_.predictedInputW);
    const double headroom = snapshot_.guardW - riskInput;
    if (headroom >= 0.10 * snapshot_.capacityW) {
        return kFastRecoveryIntervalSeconds;
    }
    if (headroom >= 0.05 * snapshot_.capacityW) {
        return kModerateRecoveryIntervalSeconds;
    }
    return kConservativeRecoveryIntervalSeconds;
}

double GovernorPolicy::targetForBand() const {
    switch (snapshot_.band) {
        case GovernorBand::Cruise:
            return config_.cruiseTargetRatio * snapshot_.capacityW;
        case GovernorBand::Guard:
            return config_.guardTargetRatio * snapshot_.capacityW;
        case GovernorBand::Shed:
            return config_.shedTargetRatio * snapshot_.capacityW;
        case GovernorBand::Emergency:
            return config_.raplFloorW;
    }
    return config_.raplFloorW;
}
