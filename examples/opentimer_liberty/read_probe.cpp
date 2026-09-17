// Small native parser probe for test_opentimer_liberty.py (no netlist needed).
#include <ot/liberty/celllib.hpp>

int main(int argc, char** argv) {
  if(argc < 2 || argc > 3) return 2;
  ot::Celllib lib;
  lib.read(argv[1]);
  if(argc == 3 && std::string(argv[2]) == "audit") {
    std::cout << std::setprecision(9) << "{\"cells\":[";
    bool first_cell = true;
    for(const auto& [name, cell] : lib.cells) {
      if(!first_cell) std::cout << ',';
      first_cell = false;
      std::cout << "{\"name\":" << std::quoted(name) << ",\"pins\":[";
      bool first_pin = true;
      for(const auto& [pin_name, pin] : cell.cellpins) {
        if(!first_pin) std::cout << ',';
        first_pin = false;
        std::string direction = pin.direction == ot::CellpinDirection::INPUT ? "input" :
                                pin.direction == ot::CellpinDirection::OUTPUT ? "output" : "other";
        std::cout << "{\"name\":" << std::quoted(pin_name) << ",\"direction\":" << std::quoted(direction)
                  << ",\"timings\":[";
        bool first_timing = true;
        for(const auto& timing : pin.timings) {
          auto different = timing;
          different.when += " different_condition";
          if(timing.isomorphic(different)) return 3;
          different = timing;
          different.sdf_cond += " different_sdf_condition";
          if(timing.isomorphic(different)) return 4;
          if(!first_timing) std::cout << ',';
          first_timing = false;
          std::cout << "{\"related_pin\":" << std::quoted(timing.related_pin)
                    << ",\"when\":" << std::quoted(timing.when)
                    << ",\"sdf_cond\":" << std::quoted(timing.sdf_cond)
                    << ",\"tables\":{";
          bool first_table = true;
          auto table = [&](const char* key, const std::optional<ot::Lut>& lut) {
            if(!lut) return;
            if(!first_table) std::cout << ',';
            first_table = false;
            auto array = [&](const std::vector<float>& values) {
              std::cout << '[';
              for(size_t i=0; i<values.size(); ++i) { if(i) std::cout << ','; std::cout << values[i]; }
              std::cout << ']';
            };
            std::cout << std::quoted(key) << ":{\"index_1\":"; array(lut->indices1);
            std::cout << ",\"index_2\":"; array(lut->indices2);
            std::cout << ",\"values\":"; array(lut->table);
            std::cout << '}';
          };
          table("cell_rise", timing.cell_rise); table("cell_fall", timing.cell_fall);
          table("rise_transition", timing.rise_transition); table("fall_transition", timing.fall_transition);
          table("rise_constraint", timing.rise_constraint); table("fall_constraint", timing.fall_constraint);
          std::cout << "}}";
        }
        std::cout << "]}";
      }
      std::cout << "]}";
    }
    std::cout << "]}\n";
    return 0;
  }
  std::cout << "cells " << lib.cells.size() << '\n';
  if(argc == 3) {
    const auto& lut = *lib.cells.at("BUF").cellpins.at("Y").timings.at(0).cell_rise;
    std::cout << "axis1";
    for(auto v : lut.indices1) std::cout << ' ' << v;
    std::cout << "\naxis2";
    for(auto v : lut.indices2) std::cout << ' ' << v;
    std::cout << "\nvalues";
    for(auto v : lut.table) std::cout << ' ' << v;
    std::cout << "\nsample " << lut(0.5f, 15.0f) << '\n';
  }
}
