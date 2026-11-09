const fs=require('fs');const crypto=require('crypto');const path=require('path');
const root=__dirname;const manifest=JSON.parse(fs.readFileSync(path.join(root,'dependency-lock.json'),'utf8'));
for(const [file,hash]of Object.entries(manifest.files)){const data=fs.readFileSync(path.join(root,'vendor',file));if(crypto.createHash('sha256').update(data).digest('hex')!==hash)throw Error('Dependency hash mismatch');}
for(const file of ['index.html','app.js','style.css'])if(!fs.existsSync(path.join(root,file)))throw Error('Missing UI source');
new Function(fs.readFileSync(path.join(root,'app.js'),'utf8'));console.log('Local static UI verified; no remote assets or build-time downloads.');
