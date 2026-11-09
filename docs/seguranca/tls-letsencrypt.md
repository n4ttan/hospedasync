# TLS com Let's Encrypt na AWS (opcional)

Sem dominio proprio o navegador recusa certificado autoassinado; para a demo use
`scripts/gen_selfsigned_cert.sh <IP> [DNS]` e aceite o aviso. Para certificado valido:

1. **Subdominio gratis**: crie uma conta em DuckDNS (ou No-IP), registre `meuapp.duckdns.org`
   apontando para o IP publico (Elastic IP) da EC2.
2. **Security Group** da EC2: liberar 80 e 443 de `0.0.0.0/0`; 22 apenas do seu IP. Nao exponha
   5432, 6379, 5672, 15672, 8000 nem as portas dos workers.
3. **Emitir o certificado** (nginx ainda parado, porta 80 livre):
   ```bash
   sudo apt install -y certbot
   sudo certbot certonly --standalone -d meuapp.duckdns.org -m voce@email.com --agree-tos -n
   ```
4. **Copiar para o diretorio montado** (`deploy/nginx/certs`):
   ```bash
   sudo cp /etc/letsencrypt/live/meuapp.duckdns.org/fullchain.pem deploy/nginx/certs/
   sudo cp /etc/letsencrypt/live/meuapp.duckdns.org/privkey.pem   deploy/nginx/certs/
   sudo chown $USER deploy/nginx/certs/*.pem
   ```
5. Suba o compose; teste `curl -I https://meuapp.duckdns.org/health` e `curl -I http://...` (deve dar 301).
6. **Renovacao** (validade 90 dias): cron semanal
   `certbot renew --deploy-hook "cp /etc/letsencrypt/live/meuapp.duckdns.org/*.pem /caminho/deploy/nginx/certs/ && docker compose restart nginx"`.
   Se a porta 80 estiver ocupada pelo nginx, use `--webroot -w /var/www/certbot` (o nginx.conf ja serve `/.well-known/acme-challenge/`; monte esse diretorio no container).
