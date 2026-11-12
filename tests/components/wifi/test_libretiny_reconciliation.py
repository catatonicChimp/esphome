"""Compile actual backend consumer methods against a small synchronous SDK mock.

Run directly with Python; no ESPHome installation or target toolchain is needed.
This tests extracted production method bodies, not the full target translation unit,
common retry state machine, SDK threading, or the queue's concurrency guarantees.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "esphome/components/wifi/wifi_component_libretiny.cpp"


def braced_body(source: str, marker: str) -> str:
    """Return a balanced block (these methods have no braces in literals)."""
    start = source.index("{", source.index(marker))
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


PRELUDE = r"""
#include <cassert>
#include <cstdint>
#include <optional>
#include <vector>
#define ESP_LOGV(...) ((void) 0)
#define ESP_LOGW(...) ((void) 0)
constexpr int WIFI_STA = 1, WL_CONNECTED = 3;
constexpr int WIFI_COMPONENT_STATE_OFF = 0, WIFI_COMPONENT_STATE_STA_CONNECTING = 1,
              WIFI_COMPONENT_STATE_STA_CONNECTED = 2;
enum class LTWiFiSTAState : uint8_t { IDLE, CONNECTING, CONNECTED, ERROR_NOT_FOUND, ERROR_FAILED };
namespace network {
constexpr int IP_ADDRESS_BUFFER_SIZE = 16;
struct IPAddress {
  uint32_t value;
  IPAddress(uint32_t v) : value(v) {}
  bool operator!=(const IPAddress &other) const { return value != other.value; }
};
}
struct ManualIP { network::IPAddress static_ip{7}; };
struct WiFiAP {
  std::optional<ManualIP> ip;
  const std::optional<ManualIP> &get_manual_ip() const { return ip; }
};
struct SDK {
  int mode{1}, status_value{3}, status_calls{0}, ip_calls{0};
  uint32_t ip{7};
  int getMode() { return mode; }
  int status() { ++status_calls; return status_value; }
  uint32_t localIP() { ++ip_calls; return ip; }
} WiFi;
struct Application {
  uint32_t now{0};
  uint32_t get_loop_component_start_time() const { return now; }
  const char *get_name() const { return "mock"; }
} App;
struct LTWiFiEvent {
  int event_id;
  struct { struct { uint8_t bssid[6]{}, ssid[33]{}, ssid_len{}, channel{}, authmode{}; } sta_connected; } data;
};
constexpr int ESPHOME_EVENT_ID_WIFI_STA_CONNECTED = 1, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP = 2;
constexpr int MAC_ADDRESS_PRETTY_BUFFER_SIZE = 18;
void format_mac_addr_upper(const uint8_t *, char *) {}
struct Queue {
  std::vector<LTWiFiEvent *> events;
  uint16_t dropped{0}, race_drop{0};
  bool push(LTWiFiEvent *event) {
    if (events.size() == 4) { ++dropped; return false; }
    events.push_back(event); return true;
  }
  LTWiFiEvent *pop() {
    if (events.empty()) { dropped += race_drop; race_drop = 0; return nullptr; }
    auto *event = events.front(); events.erase(events.begin()); return event;
  }
  uint16_t get_and_reset_dropped_count() { auto n = dropped; dropped = 0; return n; }
};
struct WiFiComponent {
  int state_{WIFI_COMPONENT_STATE_STA_CONNECTED};
  uint8_t sta_state_{static_cast<uint8_t>(LTWiFiSTAState::CONNECTED)};
  bool connected_{true}, error_from_callback_{false}, lt_teardown_event_pending_{true};
  uint32_t lt_last_link_check_{0};
  struct { bool connect_state{true}; } pending_;
  Queue event_queue_;
  WiFiAP config;
  int disconnects{0}, ips{0};
  bool wifi_sta_has_live_ip_() const;
  bool wifi_sta_can_accept_success_() const;
  void wifi_reconcile_sta_(bool overflow);
  bool wifi_loop_();
  void wifi_process_event_(LTWiFiEvent *event);
  const WiFiAP *get_selected_sta_() const { return &config; }
  void update_connected_state_() {
    connected_ = state_ == WIFI_COMPONENT_STATE_STA_CONNECTED &&
                 sta_state_ == static_cast<uint8_t>(LTWiFiSTAState::CONNECTED) && !error_from_callback_;
  }
  void notify_disconnect_state_listeners_() { assert(!connected_); ++disconnects; }
  void notify_ip_state_listeners_() { ++ips; }
};
"""

CASES = r"""
void tick(WiFiComponent &c, uint32_t now) {
  App.now = now;
  if (c.wifi_loop_() || !c.connected_) c.update_connected_state_();
}
void queue(WiFiComponent &c, int id) {
  auto *e = new LTWiFiEvent{id, {}};
  if (!c.event_queue_.push(e)) delete e;
}
int main() {
  // No events: driver loss with retained IP, cadence, notification once.
  { WiFi = {}; WiFiComponent c; WiFi.status_value = 0;
    tick(c, 999); assert(c.connected_ && WiFi.status_calls == 0);
    tick(c, 1000); assert(!c.connected_ && c.disconnects == 1);
    assert(!c.pending_.connect_state && !c.lt_teardown_event_pending_);
    tick(c, 2000); assert(c.disconnects == 1); }
  { WiFi = {}; WiFiComponent c; tick(c, 1000); assert(c.connected_);
    WiFi.ip = 0; tick(c, 2000); assert(!c.connected_); }
  { WiFi = {}; WiFiComponent c; WiFi.mode = 0; tick(c, 1000);
    assert(!c.connected_ && WiFi.status_calls == 0 && WiFi.ip_calls == 0); }
  { WiFi = {}; WiFiComponent c; c.lt_last_link_check_ = UINT32_MAX - 499;
    WiFi.status_value = 0; tick(c, 499); assert(c.connected_);
    tick(c, 500); assert(!c.connected_); }
  // Ordinary transient connecting is not polled or failed.
  { WiFi = {}; WiFiComponent c; c.state_ = WIFI_COMPONENT_STATE_STA_CONNECTING;
    c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::CONNECTING); c.connected_ = false;
    WiFi.status_value = 0; tick(c, 9999);
    assert(c.sta_state_ == static_cast<uint8_t>(LTWiFiSTAState::CONNECTING));
    assert(WiFi.status_calls == 0); }
  // Overflow even when the queue is empty, including a drop during draining.
  for (bool race : {false, true}) {
    WiFi = {}; WiFiComponent c; WiFi.status_value = 0;
    if (race) c.event_queue_.race_drop = 1; else c.event_queue_.dropped = 1;
    tick(c, 1); assert(!c.connected_ && c.disconnects == 1);
  }
  // Saturation with queued success cannot revive an incomplete attempt.
  { WiFi = {}; WiFiComponent c; c.state_ = WIFI_COMPONENT_STATE_STA_CONNECTING;
    c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::CONNECTING); c.connected_ = false;
    for (int i = 0; i < 5; ++i) queue(c, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP);
    tick(c, 1); assert(c.sta_state_ == static_cast<uint8_t>(LTWiFiSTAState::ERROR_FAILED));
    assert(!c.connected_ && c.ips == 0 && !c.pending_.connect_state); }
  { WiFi = {}; WiFiComponent c; c.event_queue_.dropped = 1; tick(c, 1); assert(c.connected_); }
  // Both queued success paths reject terminal, disabled, physically lost, and zero-IP states.
  for (int event : {ESPHOME_EVENT_ID_WIFI_STA_CONNECTED, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP}) {
    for (int failure = 0; failure < 5; ++failure) {
      WiFi = {}; WiFiComponent c; c.config.ip.emplace(); c.pending_.connect_state = false;
      if (failure == 0) c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::ERROR_FAILED);
      if (failure == 1) c.state_ = WIFI_COMPONENT_STATE_OFF;
      if (failure == 2) WiFi.status_value = 0;
      if (failure == 3) WiFi.ip = 0;
      if (failure == 4) WiFi.mode = 0;
      queue(c, event); tick(c, 1); assert(c.ips == 0 && !c.pending_.connect_state);
    }
  }
  { WiFi = {}; WiFiComponent c; c.config.ip.emplace(); WiFi.ip = 8;
    c.pending_.connect_state = false; queue(c, ESPHOME_EVENT_ID_WIFI_STA_CONNECTED);
    tick(c, 1); assert(c.ips == 0 && !c.pending_.connect_state); }
  for (int event : {ESPHOME_EVENT_ID_WIFI_STA_CONNECTED, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP}) {
    WiFi = {}; WiFiComponent c; c.config.ip.emplace();
    c.state_ = WIFI_COMPONENT_STATE_STA_CONNECTING;
    c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::CONNECTING); c.connected_ = false;
    queue(c, event); tick(c, 1);
    assert(c.sta_state_ == static_cast<uint8_t>(LTWiFiSTAState::CONNECTED));
    assert(c.pending_.connect_state && c.ips == 1);
  }
  // DHCP CONNECTED before a usable IP is ignored; GOT_IP arms the listener later.
  { WiFi = {}; WiFiComponent c; c.state_ = WIFI_COMPONENT_STATE_STA_CONNECTING;
    c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::CONNECTING); c.connected_ = false;
    c.pending_.connect_state = false; WiFi.ip = 0;
    queue(c, ESPHOME_EVENT_ID_WIFI_STA_CONNECTED); tick(c, 1); assert(!c.pending_.connect_state);
    WiFi.ip = 7; queue(c, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP); tick(c, 2);
    assert(c.pending_.connect_state && c.ips == 1); }
  // Legitimate address update and overflow never promote terminal state.
  { WiFi = {}; WiFiComponent c; queue(c, ESPHOME_EVENT_ID_WIFI_STA_GOT_IP);
    tick(c, 1); assert(c.ips == 1 && c.connected_); }
  { WiFi = {}; WiFiComponent c; c.connected_ = false;
    c.sta_state_ = static_cast<uint8_t>(LTWiFiSTAState::ERROR_FAILED);
    c.event_queue_.dropped = 1; tick(c, 1);
    assert(c.sta_state_ == static_cast<uint8_t>(LTWiFiSTAState::ERROR_FAILED)); }
}
"""


class LibreTinyReconciliationTest(unittest.TestCase):
    def test_actual_consumer_methods(self) -> None:
        compiler = shutil.which("clang++") or shutil.which("g++")
        if compiler is None:
            self.skipTest("C++ compiler required")
        source = BACKEND.read_text()
        methods = [
            signature + braced_body(source, signature)
            for signature in (
                "bool WiFiComponent::wifi_sta_has_live_ip_() const",
                "bool WiFiComponent::wifi_sta_can_accept_success_() const",
                "void WiFiComponent::wifi_reconcile_sta_(bool overflow)",
                "bool WiFiComponent::wifi_loop_()",
            )
        ]
        cases = []
        consumer = source[source.index("void WiFiComponent::wifi_process_event_") :]
        for event in ("CONNECTED", "GOT_IP"):
            marker = f"case ESPHOME_EVENT_ID_WIFI_STA_{event}:"
            cases.append(marker + braced_body(consumer, marker))
        methods.append(
            "void WiFiComponent::wifi_process_event_(LTWiFiEvent *event) { switch(event->event_id) {"
            + "\n".join(cases)
            + "} }"
        )
        with tempfile.TemporaryDirectory(prefix="esphome-link-test-") as tmp:
            cpp = Path(tmp) / "test.cpp"
            binary = Path(tmp) / "test"
            cpp.write_text(PRELUDE + "\n".join(methods) + CASES)
            # Exercise feature guards with and without listeners/manual IP.
            for features in (
                [],
                [
                    "USE_WIFI_MANUAL_IP",
                    "USE_WIFI_CONNECT_STATE_LISTENERS",
                    "USE_WIFI_IP_STATE_LISTENERS",
                ],
            ):
                # Assertions cover notification paths, so the reduced build is compile-only.
                command = [compiler, "-std=c++20", "-DUSE_RTL87XX"]
                command.extend(f"-D{feature}" for feature in features)
                subprocess.run([*command, str(cpp), "-o", str(binary)], check=True)
                if features:
                    subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
