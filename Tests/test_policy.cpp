#include "../Daemon/Policy.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <iostream>

namespace {

double advance(
    GovernorPolicy &policy,
    double start,
    double duration,
    double input,
    double capacity,
    double package
) {
    double nextSMC = start;
    const double end = start + duration;
    for (double now = start; now <= end + 1e-9; now += 0.1) {
        assert(policy.updatePackagePower(now, package));
        if (now + 1e-9 >= nextSMC) {
            assert(policy.updateSMC(now, input, capacity));
            nextSMC += 0.25;
        }
        policy.evaluate(now);
    }
    return end + 0.1;
}

void expectClose(double actual, double expected, double tolerance = 0.01) {
    assert(std::abs(actual - expected) <= tolerance);
}

PolicyConfig config() {
    PolicyConfig value;
    value.reserveW = 2.0;
    value.raplFloorW = 5.0;
    value.raplCeilingPL1W = 45.0;
    value.raplCeilingPL2W = 55.0;
    value.cruisePL2BurstW = 4.0;
    return value;
}

} // namespace

int main() {
    GovernorPolicy policy(config());
    double now = advance(policy, 0.0, 2.0, 20.0, 79.496, 5.0);
    PolicySnapshot snapshot = policy.snapshot();
    assert(snapshot.ready);
    expectClose(snapshot.guardW, 45.0026856);
    expectClose(snapshot.shedW, 51.9983336);
    expectClose(snapshot.emergencyW, 58.0002816);
    assert(snapshot.band == GovernorBand::Cruise);

    now = advance(policy, now, 3.2, 46.0, 79.496, 30.0);
    assert(policy.snapshot().band == GovernorBand::Guard);
    now = advance(policy, now, 2.2, 53.0, 79.496, 38.0);
    assert(policy.snapshot().band == GovernorBand::Shed);
    now = advance(policy, now, 0.2, 58.1, 79.496, 42.0);
    assert(policy.snapshot().band == GovernorBand::Emergency);

    now = advance(policy, now, 6.2, 40.0, 79.496, 25.0);
    assert(policy.snapshot().band == GovernorBand::Shed);
    now = advance(policy, now, 11.2, 40.0, 79.496, 25.0);
    assert(policy.snapshot().band == GovernorBand::Guard);
    now = advance(policy, now, 16.2, 40.0, 79.496, 25.0);
    assert(policy.snapshot().band == GovernorBand::Cruise);

    GovernorPolicy downscaled(config());
    advance(downscaled, 0.0, 2.0, 15.0, 60.0, 4.0);
    expectClose(downscaled.snapshot().guardW, 33.966);
    expectClose(downscaled.snapshot().shedW, 39.246);
    expectClose(downscaled.snapshot().emergencyW, 43.776);

    GovernorPolicy upscaled(config());
    advance(upscaled, 0.0, 2.0, 20.0, 100.0, 5.0);
    expectClose(upscaled.snapshot().guardW, 56.61);
    expectClose(upscaled.snapshot().shedW, 65.41);
    expectClose(upscaled.snapshot().emergencyW, 72.96);

    PolicyConfig aggressiveConfig = config();
    aggressiveConfig.raplFloorW = 9.0;
    aggressiveConfig.raplCeilingPL1W = 43.0;
    aggressiveConfig.raplCeilingPL2W = 45.0;
    aggressiveConfig.cruisePL2BurstW = 2.0;
    aggressiveConfig.guardRatio = 55.0 / 79.496;
    aggressiveConfig.shedRatio = 60.0 / 79.496;
    aggressiveConfig.emergencyRatio = 65.0 / 79.496;
    aggressiveConfig.cruiseTargetRatio = 55.0 / 79.496;
    aggressiveConfig.guardTargetRatio = 48.0 / 79.496;
    aggressiveConfig.shedTargetRatio = 42.0 / 79.496;
    GovernorPolicy aggressive(aggressiveConfig);
    double aggressiveTime = advance(aggressive, 0.0, 2.0, 44.0, 79.496, 34.0);
    expectClose(aggressive.snapshot().guardW, 55.0);
    expectClose(aggressive.snapshot().shedW, 60.0);
    expectClose(aggressive.snapshot().emergencyW, 65.0);
    expectClose(aggressive.snapshot().stateTargetW, 55.0);
    expectClose(aggressive.snapshot().desiredPL1W, 43.0);
    expectClose(aggressive.snapshot().pl1W, 43.0);
    expectClose(aggressive.snapshot().pl2W, 45.0);

    const double cruisePL1 = aggressive.snapshot().pl1W;
    aggressiveTime = advance(
        aggressive, aggressiveTime, 3.2, 56.0, 79.496, 43.0
    );
    assert(aggressive.snapshot().band == GovernorBand::Guard);
    expectClose(aggressive.snapshot().stateTargetW, 48.0);
    assert(aggressive.snapshot().pl1W < cruisePL1 - 4.0);
    const double guardPL1 = aggressive.snapshot().pl1W;

    aggressiveTime = advance(
        aggressive, aggressiveTime, 2.2, 61.0, 79.496, 45.0
    );
    assert(aggressive.snapshot().band == GovernorBand::Shed);
    expectClose(aggressive.snapshot().stateTargetW, 42.0);
    assert(aggressive.snapshot().pl1W < guardPL1 - 3.0);

    advance(aggressive, aggressiveTime, 0.2, 65.1, 79.496, 45.0);
    assert(aggressive.snapshot().band == GovernorBand::Emergency);
    expectClose(aggressive.snapshot().stateTargetW, 9.0);
    expectClose(aggressive.snapshot().pl1W, 9.0);
    expectClose(aggressive.snapshot().pl2W, 9.0);

    GovernorPolicy aggressiveScaled(aggressiveConfig);
    advance(aggressiveScaled, 0.0, 2.0, 20.0, 60.0, 8.0);
    expectClose(aggressiveScaled.snapshot().guardW, 55.0 * 60.0 / 79.496);
    expectClose(aggressiveScaled.snapshot().shedW, 60.0 * 60.0 / 79.496);
    expectClose(aggressiveScaled.snapshot().emergencyW, 65.0 * 60.0 / 79.496);

    PolicyConfig mappedConfig = config();
    mappedConfig.packageToInputSlope = 1.5;
    mappedConfig.packageToInputInterceptW = 10.0;
    GovernorPolicy mapped(mappedConfig);
    advance(mapped, 0.0, 2.0, 20.0, 79.496, 33.0);
    assert(mapped.snapshot().predictedInputW > mapped.snapshot().emergencyW);
    assert(mapped.snapshot().band == GovernorBand::Emergency);

    GovernorPolicy tiers(config());
    double tierTime = advance(tiers, 0.0, 2.0, 20.0, 79.496, 5.0);
    tierTime = advance(tiers, tierTime, 0.2, 45.0, 79.496, 30.0);
    assert(tiers.snapshot().band != GovernorBand::Emergency);
    tierTime = advance(tiers, tierTime, 2.2, 52.0, 79.496, 37.0);
    assert(tiers.snapshot().band == GovernorBand::Shed);
    advance(tiers, tierTime, 0.2, 58.1, 79.496, 42.0);
    assert(tiers.snapshot().band == GovernorBand::Emergency);
    expectClose(tiers.snapshot().pl1W, 5.0);
    expectClose(tiers.snapshot().pl2W, 5.0);

    GovernorPolicy limiter(config());
    double limitTime = advance(limiter, 0.0, 2.0, 40.0, 79.496, 5.0);
    const double lowered = limiter.snapshot().pl1W;
    limitTime = advance(limiter, limitTime, 4.8, 20.0, 79.496, 5.0);
    assert(limiter.snapshot().pl1W >= lowered + 3.0);
    const double afterFirstWindow = limiter.snapshot().pl1W;
    advance(limiter, limitTime, 0.4, 20.0, 79.496, 5.0);
    assert(limiter.snapshot().pl1W <= afterFirstWindow + 1.01);

    PolicyConfig skewConfig = config();
    skewConfig.reserveW = 9.5;
    skewConfig.raplFloorW = 9.0;
    skewConfig.raplCeilingPL1W = 37.0;
    skewConfig.raplCeilingPL2W = 37.0;
    skewConfig.cruisePL2BurstW = 10.0;
    skewConfig.packageToInputSlope = 1.205789;
    skewConfig.packageToInputInterceptW = 3.459693;
    GovernorPolicy phaseSkew(skewConfig);
    double skewTime = advance(phaseSkew, 0.0, 2.0, 13.0, 79.496, 6.0);
    assert(phaseSkew.snapshot().band == GovernorBand::Cruise);

    // Reproduce the captured phase error: PDTR rises while the instantaneous
    // package sample is still low, then package power catches up before the
    // next SMC sample. Re-evaluating the stale pair must not amplify non-CPU
    // power or manufacture an emergency transition.
    assert(phaseSkew.updateSMC(skewTime, 42.646, 79.496));
    phaseSkew.evaluate(skewTime);
    const double oncePerSMC = phaseSkew.snapshot().nonCPUW;
    for (unsigned repeat = 1U; repeat <= 4U; ++repeat) {
        phaseSkew.evaluate(skewTime + 0.05 * (double)repeat);
        expectClose(phaseSkew.snapshot().nonCPUW, oncePerSMC, 0.0001);
    }
    skewTime += 0.21;
    assert(phaseSkew.updatePackagePower(skewTime, 29.661));
    const PolicySnapshot skewed = phaseSkew.evaluate(skewTime);
    assert(skewed.band == GovernorBand::Cruise);
    assert(skewed.predictedInputW < skewed.emergencyW);
    assert(skewed.nonCPUW < 15.0);
    assert(skewed.filteredPackageW < skewed.packageW);

    PolicyConfig fallingConfig = config();
    fallingConfig.raplFloorW = 9.0;
    fallingConfig.raplCeilingPL1W = 37.0;
    fallingConfig.raplCeilingPL2W = 37.0;
    fallingConfig.cruisePL2BurstW = 10.0;
    fallingConfig.packageToInputSlope = 1.205789;
    fallingConfig.packageToInputInterceptW = 3.459693;
    GovernorPolicy fallingEdge(fallingConfig);
    double fallingTime = advance(fallingEdge, 0.0, 2.0, 14.0, 79.496, 7.0);
    fallingTime = advance(fallingEdge, fallingTime, 3.2, 46.5, 79.496, 36.0);
    assert(fallingEdge.snapshot().band == GovernorBand::Guard);
    const double steadyOtherW = fallingEdge.snapshot().nonCPUW;
    double maximumOtherW = steadyOtherW;
    double minimumPL1W = fallingEdge.snapshot().pl1W;
    bool sawAlignmentHold = false;
    bool sawAttributionGate = false;

    // Exact shape captured when the TUI load stopped: package power collapses
    // first while delayed PDTR remains high for another second. This must not
    // be interpreted as a new 30+ W non-CPU load or drive PL1 to its floor.
    double nextSMC = fallingTime;
    const double delayedInputEnd = fallingTime + 1.0;
    for (double sampleTime = fallingTime;
         sampleTime <= delayedInputEnd + 1e-9;
         sampleTime += 0.1) {
        assert(fallingEdge.updatePackagePower(sampleTime, 6.54));
        if (sampleTime + 1e-9 >= nextSMC) {
            assert(fallingEdge.updateSMC(sampleTime, 46.764, 79.496));
            nextSMC += 0.25;
        }
        const PolicySnapshot edge = fallingEdge.evaluate(sampleTime);
        sawAlignmentHold = sawAlignmentHold
            || edge.alignedPackageW > edge.filteredPackageW + 20.0;
        sawAttributionGate = sawAttributionGate
            || edge.rawNonCPUObservationW > edge.nonCPUObservationW + 10.0;
        assert(edge.band != GovernorBand::Emergency);
        maximumOtherW = std::max(maximumOtherW, edge.nonCPUW);
        minimumPL1W = std::min(minimumPL1W, edge.pl1W);
    }
    fallingTime = delayedInputEnd + 0.1;
    nextSMC = fallingTime;
    const double lowInputEnd = fallingTime + 1.5;
    for (double sampleTime = fallingTime;
         sampleTime <= lowInputEnd + 1e-9;
         sampleTime += 0.1) {
        assert(fallingEdge.updatePackagePower(sampleTime, 6.50));
        if (sampleTime + 1e-9 >= nextSMC) {
            assert(fallingEdge.updateSMC(sampleTime, 13.5, 79.496));
            nextSMC += 0.25;
        }
        const PolicySnapshot edge = fallingEdge.evaluate(sampleTime);
        sawAlignmentHold = sawAlignmentHold
            || edge.alignedPackageW > edge.filteredPackageW + 20.0;
        sawAttributionGate = sawAttributionGate
            || edge.rawNonCPUObservationW > edge.nonCPUObservationW + 10.0;
        assert(edge.band != GovernorBand::Emergency);
        maximumOtherW = std::max(maximumOtherW, edge.nonCPUW);
        minimumPL1W = std::min(minimumPL1W, edge.pl1W);
    }
    assert(sawAlignmentHold);
    assert(sawAttributionGate);
    assert(maximumOtherW < steadyOtherW + 5.0);
    assert(minimumPL1W > 20.0);

    GovernorPolicy idleSpikes(fallingConfig);
    double idleTime = advance(idleSpikes, 0.0, 4.0, 14.0, 79.496, 7.0);
    for (unsigned cycle = 0U; cycle < 20U; ++cycle) {
        idleTime = advance(idleSpikes, idleTime, 0.1, 14.0, 79.496, 25.0);
        idleTime = advance(idleSpikes, idleTime, 0.9, 14.0, 79.496, 7.0);
    }
    // Short, ordinary CPU bursts must not make the long-lived non-CPU
    // baseline disappear merely because their peaks remain in the alignment
    // history.
    assert(idleSpikes.snapshot().filteredPackageW < 10.0);
    assert(idleSpikes.snapshot().nonCPUW > 5.0);

    // Alignment may delay attribution between rails, but direct measured
    // input must retain the immediate emergency path.
    fallingTime = lowInputEnd + 0.1;
    assert(fallingEdge.updatePackagePower(fallingTime, 6.5));
    assert(fallingEdge.updateSMC(fallingTime, 58.1, 79.496));
    const PolicySnapshot measuredEmergency = fallingEdge.evaluate(fallingTime);
    assert(measuredEmergency.band == GovernorBand::Emergency);
    expectClose(measuredEmergency.pl1W, 9.0);

    GovernorPolicy recovery(config());
    advance(recovery, 0.0, 2.0, 20.0, 79.496, 10.0);
    expectClose(recovery.snapshot().recoveryIntervalS, 1.0);
    GovernorPolicy nearGuard(config());
    advance(nearGuard, 0.0, 2.0, 44.0, 79.496, 34.0);
    expectClose(nearGuard.snapshot().recoveryIntervalS, 5.0);

    // Battery-buffered allowance: while SoC sits above the 50% gate the
    // battery covers adapter deficits, so emergency input must not floor the
    // CPU; the limit rides at the cruise target regardless of the non-CPU
    // estimate, and stays inside the verified ceilings.
    GovernorPolicy buffered(config());
    buffered.updateBatteryState(64.0);
    double bufferedTime = advance(buffered, 0.0, 2.0, 20.0, 79.496, 5.0);
    assert(buffered.snapshot().batteryBuffered);
    expectClose(buffered.snapshot().batteryPercent, 64.0);
    bufferedTime = advance(buffered, bufferedTime, 0.4, 90.0, 79.496, 45.0);
    assert(buffered.snapshot().band == GovernorBand::Emergency);
    expectClose(buffered.snapshot().stateTargetW, 43.0);
    expectClose(buffered.snapshot().desiredPL1W, 43.0);
    expectClose(buffered.snapshot().pl1W, 43.0);
    expectClose(buffered.snapshot().pl2W, 47.0);
    assert(!buffered.snapshot().nonCPUOverBudget);

    // Dropping to the 50% gate hands authority straight back to the input
    // bands: the CPU lands on the emergency floor immediately.
    bufferedTime = advance(buffered, bufferedTime, 0.2, 90.0, 79.496, 45.0);
    buffered.updateBatteryState(49.9);
    bufferedTime = advance(buffered, bufferedTime, 0.1, 90.0, 79.496, 45.0);
    assert(!buffered.snapshot().batteryBuffered);
    expectClose(buffered.snapshot().desiredPL1W, 5.0);
    expectClose(buffered.snapshot().pl1W, 5.0);

    // Above the gate again, the allowance returns without waiting out the
    // recovery ramp, and it is clamped by the profile ceiling.
    buffered.updateBatteryState(51.0);
    bufferedTime = advance(buffered, bufferedTime, 0.1, 90.0, 79.496, 45.0);
    assert(buffered.snapshot().batteryBuffered);
    expectClose(buffered.snapshot().pl1W, 43.0);

    // An unknown/invalid reading disables buffering: desired/freezes revert
    // to the normal emergency path with the floor limit.
    GovernorPolicy unknownBattery(config());
    unknownBattery.updateBatteryState(60.0);
    double unknownTime = advance(unknownBattery, 0.0, 2.0, 20.0, 79.496, 5.0);
    assert(unknownBattery.snapshot().batteryBuffered);
    unknownBattery.updateBatteryState(NAN);
    unknownTime = advance(unknownBattery, unknownTime, 0.2, 90.0, 79.496, 45.0);
    assert(!unknownBattery.snapshot().batteryBuffered);
    assert(unknownBattery.snapshot().band == GovernorBand::Emergency);
    expectClose(unknownBattery.snapshot().desiredPL1W, 5.0);
    expectClose(unknownBattery.snapshot().pl1W, 5.0);

    std::cout << "policy scaling, independent state targets, balanced and aggressive tiers, causal rail alignment, hysteresis, adaptive recovery, and battery-buffered allowance tests passed\n";
    return 0;
}
