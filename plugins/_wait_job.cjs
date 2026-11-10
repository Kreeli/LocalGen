
const http = require('node:http');
const { execFileSync } = require('node:child_process');
const token = execFileSync('D:\\garbage pile\\local_image\\.venv\\Scripts\\python.exe',
  ['-c', "import sys;sys.path.insert(0,'src');from pathlib import Path;from localgen.config import load_token;print(load_token(Path(r'D:\\garbage pile\\local_image\\.runtime\\dev-localgen-studio'),create=False))"],
  { cwd: 'D:\\garbage pile\\local_image', encoding: 'utf8' }).trim();
function api(p) {
  return new Promise((resolve) => {
    const req = http.request({ host: '127.0.0.1', port: 18793, path: p, headers: { authorization: 'Bearer ' + token } }, (res) => {
      let b = ''; res.on('data', (c) => { b += c; }); res.on('end', () => resolve(b));
    });
    req.on('error', () => resolve('{}'));
    req.end();
  });
}
(async () => {
  const id = '58847302-77bd-4103-82d2-f62f8d53a6c8';
  for (let i = 0; i < 120; i++) {
    const raw = await api('/api/v1/jobs/' + id);
    let j = {};
    try { j = JSON.parse(raw); } catch {}
    if (['succeeded','failed','cancelled','interrupted'].includes(j.status)) {
      console.log(JSON.stringify({ status: j.status, progress: j.progress, artifacts: j.artifacts, error: j.error }));
      process.exit(0);
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
  console.log(JSON.stringify({ status: 'timeout' }));
})();
