# Деплой: Render (бекенд) + Vercel (сторінка)

```
Браузер (web/, Vercel) ──WebSocket /ws──► Бекенд (server/, Render) ──► OpenAI, Respeecher, Soniox
```

Слухає розпізнавання мови самого браузера, тому на сервер іде лише текст; звук відповіді повертається тим самим WebSocket.

## 1. Render: бекенд

1. Render → **New → Blueprint** → репозиторій `Humanoid-Company/voice-agent`, **гілка `alternative`**. Render підхопить `render.yaml`.
   Або вручну: **New → Web Service**, Runtime Python, гілка `alternative`:
   - Build: `pip install -r requirements-server.txt`
   - Start: `uvicorn server.app:app --host 0.0.0.0 --port $PORT`
   - Env: `PYTHON_VERSION=3.12.7`
2. Змінні середовища:

| Змінна | Значення |
|---|---|
| `OPENAI_API_KEY` | ключ OpenAI |
| `RESPEECHER_API_KEY` | ключ Respeecher; порожній — Respeecher не з'явиться на сторінці |
| `SONIOX_API_KEY` | ключ Soniox; порожній — Soniox не з'явиться |
| `WEB_SEARCH_API_KEY` | ключ Tavily для пошуку в інтернеті (той самий, що в Eva); порожній — агент каже, що не знає свіжого |
| `FRONTEND_ORIGINS` | адреса з Vercel, напр. `https://diff-voice-agent.vercel.app` (кілька — через кому) |
| `ACCESS_CODE` | код доступу. **Задайте:** без нього будь-хто з посиланням витрачає ваші ключі |
| `OPENAI_MODEL` | текстова модель OpenAI, за замовчуванням `gpt-4.1-mini` |
| `RESPEECHER_VOICES`, `SONIOX_VOICES` | голоси через кому, якщо потрібні інші за замовчуванням (див. `.env.example`) |

3. Перевірка: `https://<сервіс>.onrender.com/healthz` → `{"ok":true}`; `/api/config` покаже, які провайдери голосу підхопились і яких ключів бракує.

> Free-план Render засинає після ~15 хв без запитів, перше з'єднання «будить» його 30–60 с.

## 2. Vercel: сторінка

1. У `web/config.js` впишіть `BACKEND_URL: "https://<сервіс>.onrender.com"` (без `/` у кінці). Закомітьте в гілку `alternative`.
2. Vercel → **Add New → Project** → той самий репозиторій, гілка `alternative`:
   - **Root Directory: `web`**
   - Framework Preset: **Other**, Build Command і Output Directory порожні.
   (Production Branch проєкту в Settings → Git має бути `alternative`, інакше Vercel збере `main`.)
3. Скопіюйте адресу (`https://….vercel.app`) у `FRONTEND_ORIGINS` на Render і перезапустіть сервіс.

Разова перевірка без редеплою: `https://….vercel.app/?backend=https://інший.onrender.com`.

## Перевірка

Відкрийте сторінку в Chrome, Edge або Safari (у Firefox розпізнавання мови немає) → виберіть голос → «Почати розмову» → дозвольте мікрофон → говоріть. Спробуйте перебити: «стоп» або просто почніть свою репліку.
