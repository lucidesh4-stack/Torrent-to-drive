/**
 * Cloudflare Worker: youtube-rss-proxy.js
 * Proxies RSS requests for YouTube channels to circumvent SSLEOFError/SSL blocking.
 */

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
  const url = new URL(request.url);
  const targetUrl = url.searchParams.get('url');

  if (!targetUrl) {
    return new Response('Error: Missing url search parameter.', {
      status: 400,
      headers: {
        'Content-Type': 'text/plain',
        'Access-Control-Allow-Origin': '*'
      }
    });
  }

  // Enforce YouTube RSS URL pattern to prevent generic open-proxy abuse
  const isYouTubeRSS = targetUrl.startsWith('https://www.youtube.com/feeds/videos.xml') || 
                       targetUrl.startsWith('https://youtube.com/feeds/videos.xml');
  
  if (!isYouTubeRSS) {
    return new Response('Error: Proxy restricted only to YouTube RSS feeds.', {
      status: 403,
      headers: {
        'Content-Type': 'text/plain',
        'Access-Control-Allow-Origin': '*'
      }
    });
  }

  try {
    const headers = new Headers();
    headers.set('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
    headers.set('Accept', 'application/atom+xml,application/xml;q=0.9,*/*;q=0.8');
    headers.set('Referer', 'https://www.youtube.com/');

    const response = await fetch(targetUrl, {
      method: 'GET',
      headers: headers
    });

    const responseHeaders = new Headers(response.headers);
    responseHeaders.set('Access-Control-Allow-Origin', '*');
    responseHeaders.set('Content-Type', 'application/xml; charset=utf-8');

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders
    });
  } catch (err) {
    return new Response(`Proxy Exception: ${err.message}`, {
      status: 502,
      headers: {
        'Content-Type': 'text/plain',
        'Access-Control-Allow-Origin': '*'
      }
    });
  }
}
