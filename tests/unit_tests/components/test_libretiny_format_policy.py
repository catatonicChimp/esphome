"""Compile the production preferences open method against a FlashDB mock.

Run directly with Python; no target SDK or hardware is needed. This checks the
consumer's policy and ordering, not FlashDB recovery or physical flash safety.
"""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SOURCE = (
    Path(__file__).resolve().parents[3]
    / "esphome/components/libretiny/preferences.cpp"
)

PRELUDE = r"""
#include <cassert>
#include <cstddef>
#include <cstring>
#include <vector>
using fdb_err_t = int;
constexpr int FDB_NO_ERR = 0;
struct Database { bool not_format = false; };
std::vector<int> calls;
bool format_allowed;
int init_result, errors, successes;
#define LT_E(...) (++errors)
#define LT_I(...) (++successes)
#if defined(LT_AMB1_EXPERIMENTAL_KVS) && LT_AMB1_EXPERIMENTAL_KVS
constexpr int FDB_KVDB_CTRL_SET_NOT_FORMAT = 9;
extern "C" int amb1_preferences_sdk_format_allowed(void) {
  calls.push_back(1);
  return format_allowed;
}
void fdb_kvdb_control(Database *db, int command, void *arg) {
  assert(command == FDB_KVDB_CTRL_SET_NOT_FORMAT);
  assert(calls == std::vector<int>{1});
  calls.push_back(2);
  db->not_format = *static_cast<bool *>(arg);
}
#endif
int fdb_kvdb_init(Database *db, const char *name, const char *partition,
                  void *defaults, void *user_data) {
  assert(std::strcmp(name, "esphome") == 0);
  assert(std::strcmp(partition, "kvs") == 0);
  assert(defaults == nullptr && user_data == nullptr);
#if defined(LT_AMB1_EXPERIMENTAL_KVS) && LT_AMB1_EXPERIMENTAL_KVS
  assert((calls == std::vector<int>{1, 2}));
  assert(db->not_format == !format_allowed);
#else
  assert(calls.empty());
  assert(!db->not_format);
#endif
  calls.push_back(3);
  return init_result;
}
namespace esphome::libretiny {
struct LibreTinyPreferences { Database db; void open(); };
}
"""

CASES = r"""
int main() {
  for (bool allowed : {false, true}) {
    for (int result : {FDB_NO_ERR, -1}) {
      calls.clear(); errors = successes = 0;
      format_allowed = allowed;
      init_result = result;
      esphome::libretiny::LibreTinyPreferences prefs;
      prefs.open();
      assert(errors == (result != FDB_NO_ERR));
      assert(successes == (result == FDB_NO_ERR));
      assert(calls.back() == 3);
    }
  }
}
"""


class LibreTinyFormatPolicyTest(unittest.TestCase):
    def test_format_policy_before_init(self) -> None:
        """Unsupported SDK fixtures deliberately omit policy/control symbols."""
        compiler = shutil.which("c++")
        if compiler is None:
            self.skipTest("C++ compiler not installed")
        source = SOURCE.read_text()
        # Keep the real conditional declaration as well as the complete method.
        prologue = source.split("namespace esphome::libretiny {", 1)[0]
        prologue = prologue.replace("#ifdef USE_LIBRETINY", "", 1)
        prologue = re.sub(r"^#include .*\n", "", prologue, flags=re.MULTILINE)
        start = source.index("void LibreTinyPreferences::open() {")
        end = source.index("\nESPPreferenceObject", start)
        method = source[start:end]
        program = (
            prologue
            + PRELUDE
            + "namespace esphome::libretiny {\n"
            + method
            + "}\n"
            + CASES
        )
        for flags in (
            [],
            ["-DLT_AMB1_EXPERIMENTAL_KVS=0"],
            ["-DLT_AMB1_EXPERIMENTAL_KVS=1"],
        ):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                cpp = Path(temp) / "policy.cpp"
                binary = Path(temp) / "policy"
                cpp.write_text(program)
                subprocess.run(
                    [compiler, "-std=c++17", *flags, str(cpp), "-o", str(binary)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
