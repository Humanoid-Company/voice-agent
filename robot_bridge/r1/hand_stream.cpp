// hand_stream: keeps one DDS connection to brainco_hand_server and applies
// hand poses read from stdin, one per line:
//   <left|right> <thumb> <thumb_aux> <index> <middle> <ring> <pinky> [speed]
// All values 0..1 (0 = open, 1 = closed). Used by hand_web.py.
//
// After each new pose the command is repeated for ~1 s (like hand_cmd), then
// publishing stops, so gesture.sh / the voice agent can still drive the hands.
#include "dds/Publisher.h"
#include "dds/Subscription.h"
#include <unitree/idl/go2/MotorCmds_.hpp>
#include <unitree/idl/go2/MotorStates_.hpp>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

namespace {

constexpr int kRepeatTicks = 20;   // 20 x 50 ms = 1 s
constexpr int kTickMs = 50;

struct Target {
    std::array<float, 6> q{};
    float speed = 1.f;
    int remaining = 0;
};

float clamp01(float v) { return std::min(1.f, std::max(0.f, v)); }

std::mutex g_mu;
Target g_target[2];               // 0 = left, 1 = right
std::atomic<bool> g_running{true};

void read_stdin()
{
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string side;
        std::array<float, 6> q{};
        float speed = 1.f;
        if (!(in >> side)) continue;
        if (side == "quit") break;
        int idx = side == "left" ? 0 : side == "right" ? 1 : -1;
        if (idx < 0) { std::cerr << "bad side: " << side << std::endl; continue; }
        bool ok = true;
        for (auto& v : q) { if (!(in >> v)) { ok = false; break; } v = clamp01(v); }
        if (!ok) { std::cerr << "need 6 values" << std::endl; continue; }
        if (in >> speed) speed = clamp01(speed); else speed = 1.f;
        std::lock_guard<std::mutex> lock(g_mu);
        g_target[idx].q = q;
        g_target[idx].speed = std::max(0.05f, speed);
        g_target[idx].remaining = kRepeatTicks;
    }
    g_running = false;   // stdin closed: parent exited
}

}  // namespace

int main()
{
    unitree::robot::ChannelFactory::Instance()->Init(0, "");

    const char* names[2] = {"left", "right"};
    std::unique_ptr<unitree::robot::RealTimePublisher<unitree_go::msg::dds_::MotorCmds_>> pub[2];
    for (int h = 0; h < 2; h++) {
        pub[h] = std::make_unique<unitree::robot::RealTimePublisher<unitree_go::msg::dds_::MotorCmds_>>(
            std::string("rt/brainco/") + names[h] + "/cmd");
        pub[h]->msg_.cmds().resize(6);
    }
    std::cout << "ready" << std::endl;

    std::thread reader(read_stdin);
    reader.detach();

    while (g_running) {
        for (int h = 0; h < 2; h++) {
            Target t;
            {
                std::lock_guard<std::mutex> lock(g_mu);
                if (g_target[h].remaining <= 0) continue;
                g_target[h].remaining--;
                t = g_target[h];
            }
            for (int i = 0; i < 6; i++) {
                pub[h]->msg_.cmds()[i].q() = t.q[i];
                pub[h]->msg_.cmds()[i].dq() = t.speed;
            }
            pub[h]->unlockAndPublish();
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(kTickMs));
    }
    return 0;
}
