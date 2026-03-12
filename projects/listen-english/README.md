# English Number Listening Trainer

An English listening trainer focused on number recognition. Practice recognizing spoken English numbers with TTS audio, track your accuracy, reaction time, and error patterns.

## Features

- **Number Range Configuration** — presets for 1-20, 1-100, 100-999, 1000-9999 or custom
- **Text-to-Speech** — Web Speech API with en-US voice
- **Reaction Time Tracking** — timer starts when audio finishes playing
- **Session Statistics** — total, correct, wrong, accuracy, average time, streak
- **Analytics** — error patterns, slow numbers, error type classification, progress charts
- **Responsive Design** — works on mobile and desktop

## Tech Stack

- Vanilla HTML/CSS/JavaScript (no framework)
- Web Speech API for TTS
- Chart.js for analytics visualizations
- localStorage for persistence
- nginx for serving (Docker)

## Running Locally

Open `index.html` in a browser, or:

```bash
docker compose up -d --build
```

Then visit `http://listen.ai.jingtao.fun` (or your configured domain).

## File Structure

```
├── index.html          # Main HTML
├── css/style.css       # Styles
├── js/
│   ├── app.js          # Main app logic
│   ├── tts.js          # Speech synthesis
│   ├── timer.js        # Reaction timing
│   ├── storage.js      # localStorage persistence
│   └── analytics.js    # Error analysis & charts
├── docker-compose.yml
├── Dockerfile
└── README.md
```
