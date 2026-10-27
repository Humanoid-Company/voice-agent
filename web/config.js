// Address of the backend on Render. Set it after the first Render deploy, then redeploy on Vercel.
// Empty = the page and the backend are served from one origin (local run).
// For a one-off test you can also open the page with ?backend=https://your-service.onrender.com
window.APP_CONFIG = {
  BACKEND_URL: "https://diff-voice-agent-backend.onrender.com",
  // Nobody spoke this long: the call goes on pause (0 = never).
  IDLE_PAUSE_SECONDS: 90,
};
