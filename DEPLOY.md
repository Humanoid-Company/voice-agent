# Веб-версія для команди: Vercel (фронт) + Render (бек)

## Як це влаштовано

```
Браузер (web/, Vercel) ──WebRTC: мікрофон і звук──► OpenAI GPT-Live
        │                                                ▲
        │ SDP-оффер                                      │ sideband (інструменти)
        ▼                                                │
Бекенд (server/, Render) ────────────────────────────────┘
   └─ інструменти сесії: голос, темп, мова, ім'я, кінець розмови, вебпошук
```

- **Звук іде напряму між браузером і OpenAI.** Затримка мінімальна, а браузер сам придушує ехо (`echoCancellation`), тож агент не перебиває сам себе.
- **Ключ OpenAI лише на бекенді.** Браузер надсилає свій SDP, бекенд створює сесію й повертає SDP-відповідь.
- **Інструменти виконує бекенд.** Він під'єднується до тієї ж сесії через sideband.
- **У кожного браузера своя розмова й налаштування.** Їх розрізняє випадковий id у `localStorage`.
- **Усе зберігається лише в пам'яті сервера.** Після рестарту на Render розмова починається заново.

## 1. Render: бекенд

1. Render → **New → Blueprint** → цей репозиторій. Render підхопить `render.yaml`.
   - Або вручну: **New → Web Service**, Runtime **Python**:
     - Build: `pip install -r requirements-server.txt`
     - Start: `uvicorn server.app:app --host 0.0.0.0 --port $PORT`
     - Env: `PYTHON_VERSION=3.12.7`
2. Змінні середовища:

| Змінна | Значення |
|---|---|
| `OPENAI_API_KEY` | ключ OpenAI |
| `PUBLIC_BACKEND_URL` | `https://<ваш-сервіс>.onrender.com` (без `/` у кінці) |
| `FRONTEND_ORIGINS` | адреса з Vercel, напр. `https://voice-agent.vercel.app` (кілька — через кому) |
| `ACCESS_CODE` | пароль для команди. **Задайте обов'язково:** без нього будь-хто з посиланням витрачатиме ваш баланс OpenAI |
| `TIMEZONE` | часовий пояс для «сьогодні» й часу доби, за замовчуванням `Europe/Kyiv` |
| `WEB_SEARCH_API_KEY` | ключ Tavily для вебпошуку (той самий, що в Assistant-agent). Без нього Єва каже, що пошук недоступний |

3. Перевірка: `https://<ваш-сервіс>.onrender.com/healthz` → `{"ok":true}`.

> **Free-план Render засинає** після ~15 хв без запитів, перше відкриття сторінки «будить» його ~30–60 с. Для демо краще план Starter.

## 2. Vercel: фронт

1. У `web/config.js` впишіть адресу бекенду: `BACKEND_URL: "https://<ваш-сервіс>.onrender.com"`. Закомітьте.
2. Vercel → **Add New → Project** → цей репозиторій.
   - **Root Directory: `web`**
   - Framework Preset: **Other**, Build Command порожній, Output Directory порожній (статичні файли).
3. Після деплою скопіюйте адресу (`https://….vercel.app`) у `FRONTEND_ORIGINS` на Render і перезапустіть сервіс.

Разова перевірка з іншим бекендом без редеплою: `https://….vercel.app/?backend=https://інший.onrender.com`.

## 3. Як тестувати

1. Відкрити сторінку → ввести код доступу (якщо задано).
2. **«Почати розмову»** → дозволити мікрофон → Єва вітається → говорити. «Дякую, Єва» — пауза, «Єва, скажи» — продовжити.

## Локальний запуск веб-версії

```bash
uv sync --group server
# .env: OPENAI_API_KEY, PUBLIC_BACKEND_URL=http://localhost:8000
.venv/bin/python -m uvicorn server.app:app --port 8000      # Windows: .venv\Scripts\python.exe
# фронт: будь-який статичний сервер з папки web/, напр.
python -m http.server 5173 -d web   # і відкрити http://localhost:5173/?backend=http://localhost:8000
```

## Що відрізняється від десктопної версії

| Десктоп (`main.py`) | Веб |
|---|---|
| wake word «Єва, скажи» з локального мікрофона | кнопка «Почати розмову» (Єва одразу вітається), пауза й продовження — «Дякую, Єва» / «Єва, скажи» |
| локальний мікрофон, VAD і власний захист від ехо | браузер: WebRTC + вбудоване придушення ехо |
| налаштування в `assistant_memory.json` | налаштування в пам'яті сервера і в браузері |
