const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 80;

app.use(express.static(path.join(__dirname, 'public')));

app.listen(PORT, '0.0.0.0', () => {
  console.log(`OpenClaw Explorer running on port ${PORT}`);
});
