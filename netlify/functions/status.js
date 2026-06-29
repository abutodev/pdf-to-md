const { getStore } = require('@netlify/blobs');

exports.handler = async (event) => {
  const jobId = event.queryStringParameters?.jobId;

  if (!jobId) {
    return { statusCode: 400, body: JSON.stringify({ error: 'Missing jobId' }) };
  }

  try {
    const store = getStore('pdf-conversions');
    const result = await store.get(jobId, { type: 'json' });

    return {
      statusCode: 200,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(result || { status: 'pending' }),
    };
  } catch (err) {
    return {
      statusCode: 500,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: err.message }),
    };
  }
};
