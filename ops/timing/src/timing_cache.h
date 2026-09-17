#pragma once
#include <ot/timer/timer.hpp>

namespace dreamplace_timing_cache {
// Split: -1 = shared MIN/MAX, 0 = MIN, 1 = MAX.
void compile(const std::vector<std::pair<std::string, int>>& libraries,
             const std::string& verilog, const std::string& sdc,
             const std::string& output);
std::unique_ptr<ot::Timer> load(const std::string& input);
}
