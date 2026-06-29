const Anthropic = require('@anthropic-ai/sdk');
const busboy = require('busboy');
const { getStore } = require('@netlify/blobs');

function parseMultipart(event) {
  return new Promise((resolve, reject) => {
    const bb = busboy({
      headers: { 'content-type': event.headers['content-type'] },
    });

    const files = {};
    const fields = {};

    bb.on('file', (fieldname, file, info) => {
      const chunks = [];
      file.on('data', chunk => chunks.push(chunk));
      file.on('end', () => {
        files[fieldname] = { buffer: Buffer.concat(chunks), filename: info.filename };
      });
    });

    bb.on('field', (name, val) => { fields[name] = val; });
    bb.on('close', () => resolve({ files, fields }));
    bb.on('error', reject);

    const body = event.isBase64Encoded
      ? Buffer.from(event.body, 'base64')
      : Buffer.from(event.body || '', 'binary');

    bb.write(body);
    bb.end();
  });
}

const FORMAT_PROMPTS = {
  markdown:
    'Convert this PDF to well-structured Markdown. Preserve all headings, lists, tables, ' +
    'code blocks, and text formatting. Output only the converted Markdown — no preamble, no explanation.',
  html:
    'Convert this PDF to clean, semantic HTML. Preserve structure: headings, paragraphs, lists, ' +
    'tables, bold, italic. Output only the HTML body content — no <html>/<head>/<body> wrapper, no preamble.',
  json:
    'Convert this PDF to a structured JSON object with a "sections" array. Each section has ' +
    '"heading" (string or null), "level" (integer 0–6), and "content" (string). ' +
    'Output only valid JSON — no preamble.',
};

const EXT_MAP = { markdown: 'md', html: 'html', json: 'json' };

exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') return;

  const store = getStore('pdf-conversions');
  let jobId;

  try {
    const { files, fields } = await parseMultipart(event);
    jobId = fields['jobId'];
    if (!jobId) return;

    await store.setJSON(jobId, { status: 'processing' }, { ttl: 3600 });

    const pdfFile = files['file'];
    if (!pdfFile) {
      await store.setJSON(jobId, { status: 'error', error: 'No file uploaded' }, { ttl: 3600 });
      return;
    }

    const filename = pdfFile.filename || 'document.pdf';
    if (!filename.toLowerCase().endsWith('.pdf')) {
      await store.setJSON(jobId, { status: 'error', error: 'Only PDF files are accepted' }, { ttl: 3600 });
      return;
    }

    const format = fields['format'] || 'markdown';
    const forceOcr = fields['force_ocr'] === 'true';
    const prompt = (FORMAT_PROMPTS[format] || FORMAT_PROMPTS.markdown) +
      (forceOcr ? '\n\nThis document may be a scanned image — carefully extract all visible text even if quality is low.' : '');

    const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
    const model = process.env.ANTHROPIC_MODEL || 'claude-haiku-4-5';

    const message = await client.messages.create({
      model,
      max_tokens: 16000,
      messages: [{
        role: 'user',
        content: [
          {
            type: 'document',
            source: { type: 'base64', media_type: 'application/pdf', data: pdfFile.buffer.toString('base64') },
          },
          { type: 'text', text: prompt },
        ],
      }],
    });

    const text = message.content.find(b => b.type === 'text')?.text || '';
    const baseName = filename.replace(/\.pdf$/i, '');
    const outputFilename = `${baseName}.${EXT_MAP[format] || 'md'}`;

    await store.setJSON(jobId, { status: 'done', text, filename: outputFilename, format }, { ttl: 3600 });
  } catch (err) {
    console.error('Conversion error:', err);
    if (jobId) {
      await store.setJSON(jobId, { status: 'error', error: err.message || 'Conversion failed' }, { ttl: 3600 });
    }
  }
};
