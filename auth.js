const FORMAT='two-etf-encrypted-v1';
const ITERATIONS=600000;
const bytes=text=>Uint8Array.from(atob(text),c=>c.charCodeAt(0));
export function validateEnvelope(p){
  if(p?.format!==FORMAT||p.algorithm!=='AES-GCM'||p.kdf!=='PBKDF2-SHA256'||p.iterations!==ITERATIONS)throw Error('加密数据版本不兼容');
  if(bytes(p.salt).length!==16||bytes(p.iv).length!==12||bytes(p.ciphertext).length<16)throw Error('加密数据不完整');
  return p;
}
export async function unlockKey(password,envelope){
  validateEnvelope(envelope);
  const material=await crypto.subtle.importKey('raw',new TextEncoder().encode(password),'PBKDF2',false,['deriveKey']);
  const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:bytes(envelope.salt),iterations:ITERATIONS,hash:'SHA-256'},material,{name:'AES-GCM',length:256},false,['decrypt']);
  return {key,salt:envelope.salt};
}
export async function decryptSnapshot(envelope,session){
  validateEnvelope(envelope);
  if(envelope.salt!==session.salt)throw Error('登录密钥已更新，请重新输入密码');
  const plain=await crypto.subtle.decrypt({name:'AES-GCM',iv:bytes(envelope.iv),additionalData:new TextEncoder().encode('two-etf-pwa|encrypted-v1'),tagLength:128},session.key,bytes(envelope.ciphertext));
  return JSON.parse(new TextDecoder().decode(plain));
}
