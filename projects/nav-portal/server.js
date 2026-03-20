const express = require('express');
const Docker = require('dockerode');
const path = require('path');
const fs = require('fs');

const app = express();
const docker = new Docker({ socketPath: '/var/run/docker.sock' });
const PORT = process.env.PORT || 3000;
const SELF_CONTAINER = process.env.SELF_CONTAINER || 'nav-portal';

// Load services metadata
function loadMeta() {
  const metaPath = path.join(__dirname, 'services-meta.json');
  try {
    return JSON.parse(fs.readFileSync(metaPath, 'utf8'));
  } catch {
    return {};
  }
}

// Format uptime from container start time
function formatUptime(startedAt) {
  const start = new Date(startedAt);
  const now = new Date();
  const diffMs = now - start;
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffDay > 0) return `${diffDay} day${diffDay === 1 ? '' : 's'}`;
  if (diffHour > 0) return `${diffHour} hour${diffHour === 1 ? '' : 's'}`;
  if (diffMin > 0) return `${diffMin} min${diffMin === 1 ? '' : 's'}`;
  return `${diffSec} sec${diffSec === 1 ? '' : 's'}`;
}

// Get all running services with VIRTUAL_HOST
async function getServices() {
  const meta = loadMeta();
  const containers = await docker.listContainers({ all: false });

  const services = [];

  for (const containerInfo of containers) {
    // Skip self
    const containerName = (containerInfo.Names[0] || '').replace(/^\//, '');
    if (containerName === SELF_CONTAINER) continue;

    // Find VIRTUAL_HOST in environment
    const container = docker.getContainer(containerInfo.Id);
    const inspect = await container.inspect();
    const env = inspect.Config.Env || [];

    let virtualHost = null;
    for (const e of env) {
      if (e.startsWith('VIRTUAL_HOST=')) {
        virtualHost = e.split('=')[1];
        break;
      }
    }

    if (!virtualHost) continue;

    // Check if preview
    const isPreview = containerName.includes('preview') ||
                      virtualHost.includes('preview');

    // Get metadata enrichment
    const enrichment = meta[virtualHost] || {};

    services.push({
      name: enrichment.name || containerName,
      url: `https://${virtualHost}`,
      status: inspect.State.Status,
      uptime: formatUptime(inspect.State.StartedAt),
      category: enrichment.category || 'Other',
      description: enrichment.description || '',
      emoji: enrichment.emoji || '\uD83D\uDCE6',
      isPreview,
      containerName,
      image: containerInfo.Image,
      createdAt: inspect.Created,
    });
  }

  // Sort: Apps first, then Other, then Infrastructure; production before preview
  const categoryOrder = { Apps: 0, Other: 1, Infrastructure: 2 };
  services.sort((a, b) => {
    const catA = categoryOrder[a.category] ?? 1;
    const catB = categoryOrder[b.category] ?? 1;
    if (catA !== catB) return catA - catB;
    if (a.isPreview !== b.isPreview) return a.isPreview ? 1 : -1;
    return a.name.localeCompare(b.name);
  });

  return services;
}

// API endpoint
app.get('/api/services', async (req, res) => {
  try {
    const services = await getServices();
    res.json({
      services,
      lastUpdated: new Date().toISOString(),
    });
  } catch (err) {
    console.error('Failed to fetch services:', err.message);
    res.status(500).json({ error: 'Failed to fetch services' });
  }
});

// Serve static frontend
app.use(express.static(path.join(__dirname, 'public')));

app.listen(PORT, () => {
  console.log(`Nav portal running on port ${PORT}`);
});
