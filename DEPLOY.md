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
| `DATABASE_URL` | рядок підключення до Postgres на Neon (`postgresql://…?sslmode=require`), як в Eva: там лежать «Знання агента». Порожній — SQLite-файл, який кожен деплой на Render стирає (сторінка про це попередить) |
| `ACCESS_PASSWORD` | пароль до «Знань агента» (прочитати й змінити). Лише тут, у коді його немає. **Не заданий або порожній — знання закриті для всіх** |
| `OPENAI_MODEL` | текстова модель OpenAI, за замовчуванням `gpt-4.1-mini` |
| `RESPEECHER_VOICES`, `SONIOX_VOICES` | необов'язково: запасні голоси, якщо API провайдера не віддасть список. Звичайно голоси бекенд бере з API сам |

3. Перевірка: `https://<сервіс>.onrender.com/healthz` → `{"ok":true}`; `/api/config` покаже, які провайдери голосу підхопились і яких ключів бракує.

> **Free-план Render засинає** після ~15 хв без запитів. Щоб цього не було, бекенд кожні 10 хв сам пінгує свою публічну адресу
> (`KEEP_AWAKE_MINUTES`, `0` вимикає), а відкрита сторінка пінгує його теж. Якщо сервер усе ж заснув (перезапуск, деплой),
> сторінка чекає до хвилини, поки він прокинеться, і показує «Сервер прокидається».
>
> **Попередження про години.** Один сервіс, що не спить, з'їдає ~744 із 750 безкоштовних годин на місяць **на весь
> workspace**. Якщо в тому ж workspace уже працює бекенд Eva з тим самим самопінгом, два сервіси разом перевищать ліміт, і Render
> призупинить їх до кінця місяця. Тоді або вимкніть пінг на одному (`KEEP_AWAKE_MINUTES=0`), або беріть платний план Starter:
> він справді не засинає.

## 2. Vercel: сторінка

1. У `web/config.js` впишіть `BACKEND_URL: "https://<сервіс>.onrender.com"` (без `/` у кінці). Закомітьте в гілку `alternative`.
2. Vercel → **Add New → Project** → той самий репозиторій, гілка `alternative`:
   - **Root Directory: `web`**
   - Framework Preset: **Other**, Build Command і Output Directory порожні.
   (Production Branch проєкту в Settings → Git має бути `alternative`, інакше Vercel збере `main`.)
3. Скопіюйте адресу (`https://….vercel.app`) у `FRONTEND_ORIGINS` на Render і перезапустіть сервіс.

Разова перевірка без редеплою: `https://….vercel.app/?backend=https://інший.onrender.com`.

## База даних і «Знання агента»

Картка «Знання агента» на сторінці зберігає текст у базі на сервері, тож він однаковий для всіх пристроїв і розмов і переживає деплой. База та сама за влаштуванням, що в Eva (`server/db.py` перенесено звідти): Postgres на Neon, коли задано `DATABASE_URL`, інакше SQLite-файл.

1. Можна взяти **ту саму базу Neon, що й в Eva**: просто вставте її `DATABASE_URL` у Render цього сервісу. Таблиця тут називається `agent_about`, таблиць Eva (`kb_*`) вона не торкається. Або створіть окремий проєкт на [neon.tech](https://neon.tech) і візьміть його рядок підключення (Connect).
2. Render → Environment → `DATABASE_URL` = рядок → Save (сервіс перезапуститься).
3. Перевірка: `/healthz` показує `"knowledge": "postgres"`, `"persistent": true`. Таблицю бекенд створює сам.
4. На сторінці: картка «Знання агента» → пароль → текст → «Зберегти».

Без `DATABASE_URL` усе працює, але знання лежать у файлі, який наступний деплой зітре; сторінка показує червоне попередження.

## Перевірка

Відкрийте сторінку в Chrome, Edge або Safari (у Firefox розпізнавання мови немає) → виберіть голос → «Почати розмову» → дозвольте мікрофон → говоріть. Спробуйте перебити: «стоп» або просто почніть свою репліку.
