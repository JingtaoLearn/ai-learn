const express = require("express");
const path = require("path");
const routes = require("./routes");

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, "../client")));
app.use("/api", routes);

// SPA fallback — serve index.html for /p/:uuid routes
app.get("/p/*", (req, res) => {
  res.sendFile(path.join(__dirname, "../client/index.html"));
});

app.listen(PORT, () => {
  console.log(`Wedding prep server running on port ${PORT}`);
});
