# PDF → Markdown

A web tool that converts PDFs to Markdown, HTML, or JSON using the Claude API. Upload one or more PDFs, choose an output format, and download the converted files — all processed server-side via a Netlify Function.

## Features

- Converts PDFs to **Markdown**, **HTML**, or **JSON**
- **OCR mode** for scanned/image-based PDFs
- Batch support — upload multiple files at once
- Drag & drop or click-to-browse upload
- Per-file status tracking with download links

## Stack

- **Frontend:** Vanilla HTML/CSS/JS (single `index.html`)
- **Backend:** Netlify Function (`netlify/functions/convert.js`)
- **AI:** Anthropic Claude API (PDF document understanding)

## Setup

### Prerequisites

- [Node.js](https://nodejs.org/) (for local dev with Netlify CLI)
- An [Anthropic API key](https://console.anthropic.com/)

### Local development

```bash
npm install
ANTHROPIC_API_KEY=your_key_here netlify dev
```

Then open `http://localhost:8888`.

### Deploy to Netlify

1. Push the repo to GitHub
2. Connect it to a new Netlify site
3. Add the environment variable `ANTHROPIC_API_KEY` in **Site settings → Environment variables**
4. Deploy — Netlify will install dependencies from `package.json` automatically

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Your Anthropic API key |
| `ANTHROPIC_MODEL` | No | `claude-opus-4-8` | Override the Claude model (e.g. `claude-haiku-4-5` for faster/cheaper conversions) |

## File size limit

Netlify Functions have a **6 MB request body limit**. The UI notes a ~5 MB per-file guideline to stay safely within this.
