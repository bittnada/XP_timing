#include "timing_cache.h"
#include <fstream>
#include <limits>
#include <cstring>

// Parsed-model archive, NOT an image of Timer memory. Pointers, worker threads,
// RC, and propagated timing are deliberately not stored. Keep the schema in
// sync with every field in OpenTimer's Celllib/Module/SDC structs.
namespace dreamplace_timing_cache {
template <typename T, typename Enable = void> struct Codec;

struct Archive {
  std::fstream stream;
  bool reading;
  uint64_t remaining = 0;
  Archive(const std::string& path, bool read) : reading(read) {
    stream.open(path, std::ios::binary | (read ? std::ios::in : (std::ios::out | std::ios::trunc)));
    if (!stream) throw std::runtime_error("Cannot open timing model: " + path);
    if (read) {
      stream.seekg(0, std::ios::end); remaining = stream.tellg(); stream.seekg(0);
    }
    uint32_t endian = 0x01020304;
    if (*reinterpret_cast<unsigned char*>(&endian) != 4)
      throw std::runtime_error("Timing cache requires little-endian host");
    std::string magic = "DREAMPLACE_TIMING_MODEL_V2_IEEE754";
    auto expected = magic;
    (*this)(magic);
    if (magic != expected || sizeof(float) != 4 || !std::numeric_limits<float>::is_iec559)
      throw std::runtime_error("Incompatible timing model format");
  }
  void bytes(void* p, size_t n) {
    if (reading) {
      if (n > remaining) throw std::runtime_error("Truncated timing model");
      stream.read(static_cast<char*>(p), n); remaining -= n;
    } else stream.write(static_cast<const char*>(p), n);
    if (!stream) throw std::runtime_error("Timing model I/O failed");
  }
  template <typename... T> void operator()(T&... v) { (Codec<T>::io(*this, v), ...); }
  void finish() {
    if (reading && remaining) throw std::runtime_error("Trailing timing model data");
    if (!reading) { stream.flush(); if (!stream) throw std::runtime_error("Timing model flush failed"); }
  }
  void check_count(uint64_t n) {
    if (reading && n > remaining) throw std::runtime_error("Invalid timing model length");
  }
};

template <typename T> struct Codec<T, std::enable_if_t<std::is_arithmetic_v<T>>> {
  static void io(Archive& a, T& x) { a.bytes(&x, sizeof(x)); }
};
template <typename T> struct Codec<T, std::enable_if_t<std::is_enum_v<T>>> {
  static void io(Archive& a, T& x) { int32_t v = int32_t(x); a(v); if (a.reading) x = T(v); }
};
template <> struct Codec<std::string> {
  static void io(Archive& a, std::string& s) {
    uint64_t n = s.size(); a(n); a.check_count(n);
    if (a.reading) s.resize(n);
    a.bytes(s.data(), n);
  }
};
template <typename T> struct Codec<std::optional<T>> {
  static void io(Archive& a, std::optional<T>& v) {
    uint8_t present = bool(v); a(present);
    if (present > 1) throw std::runtime_error("Invalid optional tag");
    if (a.reading) { if (present) v.emplace(); else v.reset(); }
    if (present) a(*v);
  }
};
template <typename T> struct Codec<std::vector<T>> {
  static void io(Archive& a, std::vector<T>& v) {
    uint64_t n = v.size(); a(n); a.check_count(n);
    if (a.reading) v.resize(n);
    if constexpr (std::is_same_v<T, float>) a.bytes(v.data(), n * sizeof(float));
    else for (auto& x : v) a(x);
  }
};
template <typename T, size_t N> struct Codec<std::array<T, N>> {
  static void io(Archive& a, std::array<T, N>& v) { for (auto& x : v) a(x); }
};
template <typename T> struct Codec<std::unordered_map<std::string, T>> {
  static void io(Archive& a, std::unordered_map<std::string, T>& v) {
    uint64_t n = v.size(); a(n); a.check_count(n);
    if (a.reading) {
      v.clear(); v.reserve(n);
      for (uint64_t i = 0; i < n; ++i) {
        std::string key; T value; a(key, value);
        if (!v.emplace(std::move(key), std::move(value)).second)
          throw std::runtime_error("Duplicate archive map key");
      }
    } else for (auto& kv : v) { auto key = kv.first; a(key, kv.second); }
  }
};
template <typename... T> struct Codec<std::variant<T...>> {
  using V = std::variant<T...>;
  template <size_t I = 0> static void select(V& v, uint32_t i) {
    if constexpr (I < sizeof...(T)) {
      if (i == I) v.template emplace<I>(); else select<I + 1>(v, i);
    } else throw std::runtime_error("Invalid archive variant tag");
  }
  static void io(Archive& a, V& v) {
    uint32_t i = v.index(); a(i); if (a.reading) select(v, i);
    std::visit([&](auto& x) { a(x); }, v);
  }
};

#define FIELDS(TYPE, ...) template <> struct Codec<TYPE> { static void io(Archive& a, TYPE& x) { a(__VA_ARGS__); } };
#define EMPTY(TYPE) template <> struct Codec<TYPE> { static void io(Archive&, TYPE&) {} };
#define UNIT(TYPE) template <> struct Codec<TYPE> { static void io(Archive& a, TYPE& x) { double v=x.value(); a(v); if(a.reading) x=TYPE(v); } };
UNIT(ot::second_t) UNIT(ot::watt_t) UNIT(ot::ohm_t)
UNIT(ot::farad_t) UNIT(ot::ampere_t) UNIT(ot::volt_t)

FIELDS(ot::LutTemplate, x.name, x.variable1, x.variable2, x.indices1, x.indices2)
// Lut::name identifies its template; the pointer is rebound after deserialization.
FIELDS(ot::Lut, x.name, x.indices1, x.indices2, x.table)
FIELDS(ot::Timing, x.related_pin, x.when, x.sdf_cond, x.sense, x.type, x.cell_rise, x.cell_fall,
       x.rise_transition, x.fall_transition, x.rise_constraint, x.fall_constraint)
FIELDS(ot::Cellpin, x.name, x.original_pin, x.direction, x.capacitance,
       x.max_capacitance, x.min_capacitance, x.max_transition, x.min_transition,
       x.fall_capacitance, x.rise_capacitance, x.fanout_load, x.max_fanout,
       x.min_fanout, x.is_clock, x.timings)
FIELDS(ot::Cell, x.name, x.cell_footprint, x.leakage_power, x.area, x.cellpins)
FIELDS(ot::Celllib, x.name, x.delay_model, x.time_unit, x.power_unit,
       x.resistance_unit, x.capacitance_unit, x.current_unit, x.voltage_unit,
       x.default_cell_leakage_power, x.default_inout_pin_cap,
       x.default_input_pin_cap, x.default_output_pin_cap, x.default_fanout_load,
       x.default_max_fanout, x.default_max_transition, x.lut_templates, x.cells)
FIELDS(ot::vlog::Gate, x.name, x.cell, x.cellpin2net, x.net2cellpin)
FIELDS(ot::vlog::Module, x.name, x.ports, x.wires, x.inputs, x.outputs, x.gates)
EMPTY(ot::sdc::CurrentDesign) EMPTY(ot::sdc::AllClocks)
EMPTY(ot::sdc::AllInputs) EMPTY(ot::sdc::AllOutputs)
EMPTY(ot::sdc::GetLibCells) EMPTY(ot::sdc::GetLibPins) EMPTY(ot::sdc::AllRegisters)
FIELDS(ot::sdc::GetClocks, x.clocks) FIELDS(ot::sdc::GetPorts, x.ports)
FIELDS(ot::sdc::GetPins, x.pins) FIELDS(ot::sdc::GetCells, x.cells)
FIELDS(ot::sdc::GetNets, x.nets) FIELDS(ot::sdc::GetLibs, x.libs)
FIELDS(ot::sdc::SetInputDelay, x.clock, x.clock_fall, x.level_sensitive,
       x.add_delay, x.network_latency_included, x.source_latency_included,
       x.min, x.max, x.rise, x.fall, x.delay_value, x.port_pin_list)
FIELDS(ot::sdc::SetInputTransition, x.clock, x.min, x.max, x.rise, x.fall,
       x.clock_fall, x.transition, x.port_list)
FIELDS(ot::sdc::SetOutputDelay, x.clock, x.clock_fall, x.level_sensitive,
       x.rise, x.fall, x.max, x.min, x.add_delay, x.network_latency_included,
       x.source_latency_included, x.delay_value, x.port_pin_list)
FIELDS(ot::sdc::SetLoad, x.min, x.max, x.subtract_pin_load, x.pin_load,
       x.wire_load, x.value, x.objects)
FIELDS(ot::sdc::CreateClock, x.period, x.add, x.name, x.comment, x.waveform, x.port_pin_list)
FIELDS(ot::sdc::SDC, x.commands)
#undef FIELDS
#undef EMPTY
#undef UNIT

void bind_templates(ot::Celllib& lib, const std::string& prefix = "") {
  if (!prefix.empty()) {
    decltype(lib.lut_templates) templates;
    for (auto& kv : lib.lut_templates) {
      auto name = prefix + kv.first;
      kv.second.name = name;
      templates.emplace(name, std::move(kv.second));
    }
    lib.lut_templates = std::move(templates);
  }
  for (auto& cell : lib.cells) for (auto& pin : cell.second.cellpins)
    for (auto& t : pin.second.timings)
      for (auto* lut : {&t.cell_rise, &t.cell_fall, &t.rise_transition,
                       &t.fall_transition, &t.rise_constraint, &t.fall_constraint}) {
        if (!*lut) continue;
        auto& l = **lut;
        if (!prefix.empty() && l.name != "scalar") l.name = prefix + l.name;
        l.lut_template = lib.lut_template(l.name);
        if (!l.lut_template && l.name != "scalar")
          throw std::runtime_error("Missing LUT template: " + l.name);
      }
}

void compile(const std::vector<std::pair<std::string, int>>& libraries,
             const std::string& verilog, const std::string& sdc,
             const std::string& output) {
  if (libraries.empty() || verilog.empty()) throw std::runtime_error("LIB and Verilog required");
  Archive a(output, false);
  uint64_t n = libraries.size(); a(n);
  std::unordered_set<std::string> masters[2];
  for (size_t i = 0; i < libraries.size(); ++i) {
    int32_t split = libraries[i].second;
    if (split < -1 || split > 1) throw std::runtime_error("Invalid library corner");
    ot::Celllib lib; lib.read(libraries[i].first);
    if (lib.cells.empty()) throw std::runtime_error("Empty cell library: " + libraries[i].first);
    for (int el = 0; el < 2; ++el) if (split == -1 || split == el)
      for (const auto& cell : lib.cells) if (!masters[el].insert(cell.first).second)
        throw std::runtime_error("Duplicate cell in one corner: " + cell.first + " (" + libraries[i].first + ")");
    // Different files may reuse a template name with different axes/units.
    bind_templates(lib, "cache_lib_" + std::to_string(i) + "_");
    a(split, lib);
  }
  if (masters[0].empty() || masters[1].empty()) throw std::runtime_error("Both MIN and MAX libraries required");
  {
    auto module = ot::vlog::read_verilog(verilog);
    for (const auto& gate : module.gates) for (int el = 0; el < 2; ++el)
      if (!masters[el].count(gate.cell)) throw std::runtime_error("Missing library cell: " + gate.cell);
    a(module);
  }
  ot::sdc::SDC constraints;
  // Never overwrite a user's design.json next to design.sdc. The archive path
  // is a unique generation allocated by PlaceDB, including for parallel jobs.
  if (!sdc.empty()) constraints.read_with_output(sdc, output + ".sdc.json");
  a(constraints);
  a.finish();
}
} // namespace dreamplace_timing_cache

namespace ot {
struct TimingCacheAccess {
  static std::unique_ptr<Timer> load(const std::string& path) {
    using namespace dreamplace_timing_cache;
    Archive a(path, true);
    auto timer = std::make_unique<Timer>();
    uint64_t n = 0; a(n); a.check_count(n);
    if (!n) throw std::runtime_error("Empty timing model");
    for (uint64_t i = 0; i < n; ++i) {
      int32_t split = 0; Celllib lib; a(split, lib); bind_templates(lib);
      if (split == -1) {
        auto early = lib; bind_templates(early);
        timer->_merge_celllib(early, MIN);
        timer->_merge_celllib(lib, MAX);
      } else if (split == MIN || split == MAX) timer->_merge_celllib(lib, Split(split));
      else throw std::runtime_error("Invalid cached library corner");
    }
    {
      vlog::Module module; a(module); timer->_verilog(module);
    }
    sdc::SDC constraints; a(constraints); a.finish();
    timer->_read_sdc(constraints);
    // Mark the reconstructed graph as pending using OpenTimer's normal lineage.
    timer->_add_to_lineage(timer->_taskflow.emplace([] {}));
    return timer;
  }
};
}
namespace dreamplace_timing_cache {
std::unique_ptr<ot::Timer> load(const std::string& input) { return ot::TimingCacheAccess::load(input); }
}
