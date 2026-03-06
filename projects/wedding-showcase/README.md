# Wedding Showcase

Static website showcasing a wedding garden party layout plan. Designed to present decoration ideas and zone arrangements to a wedding planning company.

## Overview

A responsive single-page website featuring:

- **Hero section** with event theme introduction
- **Venue floor plan** showing the linear venue layout and visitor flow
- **6 themed zones** with detailed descriptions and decoration elements
- **Atmosphere section** with overall style keywords and design elements

## Zones

| # | Zone | Description |
|---|------|-------------|
| 1 | Welcome Area | Guest sign-in wall, wristbands, raffle tickets |
| 2 | Polaroid Photo Area | Instant cameras, photo backdrop, props |
| 3 | Drink & Dessert Bar | Cocktail station, champagne tower, desserts |
| 4 | Ring Toss Game | Classic fair game with fruit and toy prizes |
| 5 | DIY Craft Area | Handmade mosquito repellent sachet workshop |
| 6 | Clown Interaction | Clown performance, balloon art |

## Tech Stack

- Pure HTML + CSS + vanilla JS (scroll animations)
- nginx:alpine Docker image for serving
- Responsive design (mobile-friendly)
- Google Fonts: Noto Serif SC, Noto Sans SC

## Deployment

```bash
docker compose up -d
```

Requires nginx-proxy environment variables:

- `S_DOMAIN` - Base domain (auto-configured as `wedding.${S_DOMAIN}`)

The service integrates with the nginx-proxy + acme-companion stack for automatic HTTPS.

## File Structure

```
wedding-showcase/
├── index.html          Main page
├── styles.css          Stylesheet
├── Dockerfile          nginx:alpine based image
├── docker-compose.yml  Deployment configuration
├── .dockerignore       Docker build exclusions
└── README.md           This file
```
