#!/bin/bash
# Один раз на Jetson R1 EDU (з мака, по кабелю):
#   scp -r robot_bridge unitree@192.168.123.164:~/
#   ssh unitree@192.168.123.164 'bash ~/robot_bridge/r1/install.sh'
#
# Після цього все стартує саме з увімкненням робота:
#   brainco-hands  — сервер рук (brainco_hand_server -n eth10)
#   robot-bridge   — міст до сайту Єви (кисті на сайті: «Роботи» → R1 EDU → «Руки»)
# Потрібен інтернет на Jetson (Wi-Fi): міст сам підключається до бекенда на Render.
set -e
BRIDGE="$(cd "$(dirname "$0")/.." && pwd)"
SVC="$HOME/brainco_hand_service"
ID="${ROBOT_ID:-r1-edu}"
NAME="${ROBOT_NAME:-R1 EDU}"
RELAY="${ROBOT_RELAY:-wss://voice-agent-f3m4.onrender.com/ws/robot}"

[ -d "$SVC" ] || { echo "Не знайдено $SVC — запускайте на Jetson робота."; exit 1; }

echo "== 1/4 Збираю hand_stream (одне з'єднання з руками замість hand_cmd на кожен рух)"
cp "$BRIDGE/r1/hand_stream.cpp" "$SVC/test/hand_stream.cpp"
grep -q "hand_stream" "$SVC/CMakeLists.txt" || printf '\nadd_executable(hand_stream test/hand_stream.cpp)\n' >> "$SVC/CMakeLists.txt"
mkdir -p "$SVC/build" && cd "$SVC/build" && cmake .. > /dev/null && make hand_stream
ls -la "$SVC/bin/hand_stream"

echo "== 2/4 Python-бібліотека websockets"
if ! sudo python3 -c "import websockets" 2>/dev/null; then
  sudo python3 -m pip --version >/dev/null 2>&1 || sudo apt-get install -y python3-pip
  sudo python3 -m pip install "websockets>=10,<14"
fi

echo "== 3/4 Автозапуск"
sudo tee /etc/systemd/system/brainco-hands.service > /dev/null <<EOF
[Unit]
Description=BrainCo hand server for R1 EDU (rt/brainco/*)
After=network.target

[Service]
WorkingDirectory=$SVC/bin
# чекаємо, поки з'являться порти рук (USB-C на спині)
ExecStartPre=/bin/sh -c 'for i in \$(seq 60); do [ -e /dev/ttyHAND0 ] && exit 0; sleep 2; done; echo "no /dev/ttyHAND0"; exit 1'
ExecStart=$SVC/bin/brainco_hand_server -n eth10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/robot-bridge.service > /dev/null <<EOF
[Unit]
Description=Eva robot bridge: R1 EDU hands -> site
After=network-online.target brainco-hands.service
Wants=network-online.target

[Service]
WorkingDirectory=$BRIDGE
Environment=HAND_STREAM=$SVC/bin/hand_stream
Environment=ROBOT_RELAY=$RELAY
Environment=ROBOT_TOKEN=${ROBOT_TOKEN:-}
ExecStart=/usr/bin/python3 $BRIDGE/bridge.py --robot r1-hands --id $ID --name "$NAME"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now brainco-hands robot-bridge

echo "== 4/4 Перевірка"
sleep 4
systemctl --no-pager --lines=0 status brainco-hands robot-bridge | grep -E "●|Active:"
if curl -s -m 8 -o /dev/null -w "%{http_code}" "${RELAY/wss:/https:}" | grep -qE "^[1-5]"; then
  echo "Інтернет є: відкрийте сайт Єви → «Роботи» → $NAME → «Руки»."
else
  echo "Немає зв'язку з $RELAY. Підключіть Jetson до Wi-Fi:"
  echo "   nmcli dev wifi list"
  echo "   sudo nmcli dev wifi connect \"НАЗВА\" password \"ПАРОЛЬ\""
  echo "Міст підключиться сам, щойно з'явиться інтернет."
fi
echo "Логи: journalctl -u robot-bridge -f    (руки: journalctl -u brainco-hands -f)"
