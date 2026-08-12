# Control Plane Docker Deployment

Este directorio contiene el despliegue **solo del Control Plane**.

El Worker Agent ya no vive en este compose. El worker se despliega junto con
los servicios que se quieren backupear en `deploy/worker/`.

## Archivos

- `docker-compose.yml`: construye localmente la imagen dedicada de `control-plane`.
- `docker-compose.ghcr.yml`: consume la imagen publicada en GHCR para `control-plane`.

## Comportamiento

- `control-plane`
  - ejecuta `python -m src.control_plane.main`
  - persiste `control_plane.db`, `.control_plane.key`, `.control_plane.session.key` y `.control_plane.users.json` en un volumen Docker
  - publica la UI/API en el puerto definido por `CONTROL_PLANE_PUBLISHED_PORT`

## Arranque

```bash
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

## Verificacion rapida

Salud del Control Plane:

```bash
curl http://127.0.0.1:18080/healthz
```

Primer login:

- usuario: `admin`
- contrasenia inicial: `changeme`
- el sistema obliga a cambiarla en el primer acceso

```bash
curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

Listar workers una vez cambiada la contrasenia:

```bash
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

## Uso con imagenes publicadas en GHCR

```bash
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Imagen esperada:

- `ghcr.io/danielrondongarcia/docker-volume-backup-control-plane`

## Enrolamiento seguro opcional

El stack de ejemplo sigue arrancando en HTTP para laboratorio rapido, pero el backend ya soporta un modo seguro inspirado en el emparejamiento tipo Wazuh:

- `CONTROL_PLANE_TLS_ENABLED=true`: genera o reutiliza una CA local bajo `CONTROL_PLANE_TLS_DIR` y publica el CP por HTTPS
- `CONTROL_PLANE_WORKER_MTLS_REQUIRED=true`: obliga a que las rutas operativas del worker usen el certificado cliente emitido por el CP
- `POST /api/v1/admin/worker-enrollments`: crea un token de un solo uso, asigna `worker_id`, devuelve `ca_certificate_pem` y la huella del certificado servidor
- `WORKER_ENROLLMENT_TOKEN`: habilita auto-enrolamiento del worker en el arranque
- `WORKER_ENROLLMENT_CA_PEM`: permite bootstrappear la confianza del worker antes de que exista su certificado cliente
- `WORKER_TLS_DIR`, `WORKER_TLS_CA_FILE`, `WORKER_TLS_CERT_FILE`, `WORKER_TLS_KEY_FILE`: controlan donde persistir la identidad TLS local del worker

Flujo resumido:

1. Un admin crea el enrolamiento desde el Control Plane.
2. El operador entrega al worker el token y el PEM de la CA.
3. El worker genera su keypair local, envia CSR a `POST /api/v1/worker-enrollments/sign` y guarda `ca/cert/key`.
4. A partir de ahi, `heartbeat`, `inventory` y fetch/update de jobs usan mTLS con la huella persistida en el worker del CP.

## Apagado

```bash
docker compose -f deploy/control-plane/docker-compose.yml down
```

## Notas

- Si `8080` ya esta ocupado en el host, publica el stack en otro puerto usando `CONTROL_PLANE_PUBLISHED_PORT`.
- Para que el worker se registre, despliegalo desde `deploy/worker/` apuntando `CONTROL_PLANE_URL` a este Control Plane.
- Este despliegue esta orientado a validacion local y laboratorio; `mTLS` ya existe de forma opcional, pero el compose de referencia no lo activa por defecto.