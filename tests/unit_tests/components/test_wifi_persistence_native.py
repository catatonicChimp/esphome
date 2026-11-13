"""Compile the production save methods and portal handler against host mocks.

No platform SDK is needed. The threaded portal deliberately acknowledges with
202, without retaining the request or exposing a persistence status endpoint.
"""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("esp8266", [False, True])
def test_wifi_persistence_and_portal_lifetime(tmp_path: Path, esp8266: bool) -> None:
    """Exercise real method bodies, including disconnect before deferred work."""
    if (compiler := shutil.which("clang++") or shutil.which("g++")) is None:
        pytest.skip("A host C++ compiler is required")
    wifi = (ROOT / "esphome/components/wifi/wifi_component.cpp").read_text()
    wifi_methods = wifi[
        wifi.index("void WiFiComponent::save_wifi_sta(") : wifi.index(
            "void WiFiComponent::connect_soon_()"
        )
    ]
    header = (ROOT / "esphome/components/wifi/wifi_component.h").read_text()
    declarations = header[
        header.index("  void save_wifi_sta(") : header.index("  // ========== INTERNAL METHODS")
    ]
    portal = (ROOT / "esphome/components/captive_portal/captive_portal.cpp").read_text()
    portal_method = portal[
        portal.index("void CaptivePortal::handle_wifisave(") : portal.index(
            "void CaptivePortal::setup()"
        )
    ]
    source = r'''
#include <cassert>
#include <cstring>
#include <functional>
#include <string>
#include <vector>
#define ESPHOME_F(x) x
std::vector<std::string> logs;
#define ESP_LOGI(tag, message, ...) logs.emplace_back(message)
#define ESP_LOGW(tag, message, ...) logs.emplace_back(message)
struct StringRef {
  const char *value;
  const char *c_str() const { return value; }
};
struct SavedWifiSettings { char ssid[33]; char password[65]; };
struct Preferences {
  bool save_result{true}, sync_result{true};
  int saves{0}, syncs{0};
  SavedWifiSettings stored{};
  bool save(SavedWifiSettings *value) { ++saves; stored = *value; return save_result; }
  bool sync() { ++syncs; return sync_result; }
} preferences;
Preferences *global_preferences = &preferences;
struct WiFiAP {
  std::string ssid, password;
  void set_ssid(const char *value) { ssid = value; }
  void set_password(const char *value) { password = value; }
};
namespace wifi {
class WiFiComponent {
 public:
''' + declarations + r'''
  Preferences &pref_ = preferences;
  WiFiAP sta;
  int connects{0};
  void set_sta(const WiFiAP &value) { sta = value; }
  void connect_soon_() { ++connects; }
};
''' + wifi_methods + r'''
WiFiComponent *global_wifi_component;
}
struct AsyncWebServerRequest {
  std::string ssid{"private-network"}, password{"private-password"};
  int code{0};
  std::string body;
  std::string arg(const char *name) { return std::string(name) == "ssid" ? ssid : password; }
  void send(int status, const char *, const char *text) { code = status; body = text; }
};
class CaptivePortal {
 public:
  std::vector<std::function<void()>> pending;
  void defer(std::function<void()> callback) { pending.push_back(callback); }
  void handle_wifisave(AsyncWebServerRequest *request);
};
''' + portal_method + r'''
int main() {
  wifi::WiFiComponent component;
  wifi::global_wifi_component = &component;
  for (bool save : {false, true}) {
    for (bool sync : {false, true}) {
      preferences = Preferences{};
      preferences.save_result = save;
      preferences.sync_result = sync;
      const int before = component.connects;
      assert(component.save_wifi_sta_with_result("new-network", "new-password") == (save && sync));
      assert(preferences.saves == 1 && preferences.syncs == 1);
      assert(component.connects == before + 1);
      assert(component.sta.ssid == "new-network" && component.sta.password == "new-password");
      assert(std::string(preferences.stored.ssid) == "new-network");
      assert(std::string(preferences.stored.password) == "new-password");
      CaptivePortal portal;
      auto *request = new AsyncWebServerRequest;
      const int saves_before = preferences.saves;
      portal.handle_wifisave(request);
#ifdef USE_ESP8266
      assert(request->code == (save && sync ? 200 : 500));
      assert(portal.pending.empty());
#else
      assert(request->code == 202);
      assert(request->body == "Connection requested; persistence not yet confirmed.");
      assert(preferences.saves == saves_before);
      assert(portal.pending.size() == 1);
#endif
      assert(request->body.find("private-") == std::string::npos);
      delete request;  // The HTTP client disconnects before the main loop runs.
      for (auto &callback : portal.pending) callback();
      assert(preferences.saves == saves_before + 1);
      assert(component.connects == before + 2);
      assert(component.sta.ssid == "private-network" && component.sta.password == "private-password");
#ifndef USE_ESP8266
      assert(logs.back() == (save && sync ? "WiFi settings saved; runtime connection requested"
                                        : "WiFi settings persistence failed; runtime connection requested"));
#endif
      for (const auto &log : logs) assert(log.find("private-") == std::string::npos);
    }
  }
  component.save_wifi_sta(std::string("legacy"), std::string("password"));
  component.save_wifi_sta(StringRef{"legacy"}, StringRef{"password"});
  assert(component.save_wifi_sta_with_result(std::string("result"), std::string("password")));
  assert(component.save_wifi_sta_with_result(StringRef{"result"}, StringRef{"password"}));
}
'''
    cpp = tmp_path / "wifi_persistence.cpp"
    executable = tmp_path / "wifi_persistence"
    cpp.write_text(source)
    command = [compiler, "-std=c++20", "-fsanitize=address,undefined", "-g"]
    if esp8266:
        command.append("-DUSE_ESP8266")
    subprocess.run([*command, str(cpp), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
