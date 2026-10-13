// Address of the backend on Render. Set it after the first Render deploy, then redeploy on Vercel.
// For a one-off test you can also open the page with ?backend=https://your-service.onrender.com
window.APP_CONFIG = {
  BACKEND_URL: "https://eva-voice-backend.onrender.com",
  // GPT-Live bills every open second. A pause closes the session after this many seconds
  // (not while a tool still runs); silence this long during a call pauses her.
  PAUSE_CLOSE_SECONDS: 30,
  IDLE_PAUSE_SECONDS: 90,
  // Approximate GPT-Live price per open minute, for the call meter under the status line.
  PRICE_PER_MINUTE_USD: 0.05,
  // Realtime: a muted call costs nothing, a reopened one reads the prompt again — pause keeps it longer.
  REALTIME_PAUSE_CLOSE_SECONDS: 600,
  // Realtime bills tokens, not minutes: $ per 1M tokens of gpt-realtime-2.1 (change with the model).
  REALTIME_PRICES_PER_1M: { text_in: 4, audio_in: 32, cached_in: 0.4, text_out: 24, audio_out: 64 },
};
