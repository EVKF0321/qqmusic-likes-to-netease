// No HTTP listener. Credentials arrive only through private stdin.
const path = require('path');
const fs = require('fs');
const tokenPath = path.join(require('os').tmpdir(), 'anonymous_token');
if (!fs.existsSync(tokenPath)) fs.writeFileSync(tokenPath, '', {flag: 'wx'});
const root = path.dirname(require.resolve('@neteasecloudmusicapienhanced/api'));
const allowed = new Set(['login_qr_key', 'login_qr_create', 'login_qr_check', 'login_status',
  'user_playlist', 'playlist_detail', 'likelist', 'song_detail', 'cloudsearch', 'like', 'song_order_update']);
let input = '';
let upstreamHint = null;
// Upstream may replace a rejected business status with 502. Keep numeric evidence
// before that transformation; suppress raw upstream logging (it may contain cookies).
console.log = (...args) => {
  if (args[0] !== '[ERR]') return;
  const obj = args[1];
  const status = Number(obj?.status), code = Number(obj?.body?.code);
  if (Number.isFinite(code) && code !== 502) upstreamHint = {status, code};
};
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 8000000) process.exit(2); });
process.stdin.on('end', async () => {
  try {
    const {method, params} = JSON.parse(input);
    if (!allowed.has(method) || (method === 'like' && params.like !== 'true')) throw Error('Operation forbidden');
    const {cookieToJson} = require(path.join(root, 'util'));
    const request = require(path.join(root, 'util/request'));
    const handler = require(path.join(root, 'module', method + '.js'));
    const result = await handler({...params, randomCNIP: false, checkToken: false, timeout: 30000, cookie: cookieToJson(params.cookie || '')}, request);
    process.stdout.write('\nMB_RESULT:' + JSON.stringify(result.body) + '\n');
  } catch (e) {
    // Return only numeric statuses and allowlisted categories, never raw messages/config/cookies.
    const status = Number(upstreamHint?.status || e?.response?.status || e?.status);
    const upstream = Number(upstreamHint?.code || e?.body?.code);
    const text = String(e?.body?.msg || e?.message || '').toLowerCase();
    let kind = 'upstream';
    if ([301, 302, 401].includes(upstream) || [401].includes(status)) kind = 'auth';
    else if ([403, 405, 429].includes(upstream) || [403, 429].includes(status)) kind = 'restricted';
    else if (/timeout|timed out|etimedout|econnaborted/.test(text)) kind = 'timeout';
    else if (/econnreset|socket hang up|eai_again|enotfound|econnrefused|network/.test(text)) kind = 'network';
    else if (e?.code === 'MODULE_NOT_FOUND') kind = 'dependency';
    const detail = {kind};
    if (Number.isFinite(status) && status >= 100 && status <= 599) detail.http_status = status;
    if (Number.isFinite(upstream)) detail.upstream_code = upstream;
    process.stdout.write('\nMB_RESULT:' + JSON.stringify({bridge_error: true, detail}) + '\n');
    process.exitCode = 1;
  }
});
