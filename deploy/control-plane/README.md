# Control Plane — Despliegue Docker

Este directorio contiene el despliegue **solo del Control Plane**.

El Worker Agent se despliega por separado, junto con los servicios que se
quieren backupear, en `deploy/worker/`.

## Archivos

- `docker-compose.yml`: construye localmente la imagen dedicada de
  `control-plane`.
- `docker-compose.ghcr.yml`: consume la imagen publicada en GHCR para
  `control-plane`.

## Comportamiento

- `control-plane`
  - ejecuta `python -m src.control_plane.main`
  - persiste `control_plane.db`, `.control_plane.key`,
    `.control_plane.session.key` y `.control_plane.users.json` en un volumen
    Docker nombrado `control_plane_state`
  - publica la UI/API en el puerto definido por `CONTROL_PLANE_PUBLISHED_PORT`
    (por defecto `8080`) y mantiene `8080` interno dentro de la red del Compose

## Red compartida con el Worker

El Compose del Control Plane crea una red llamada
`docker-volume-backup-control-plane_default`. El Compose del Worker se conecta
a esa red como red externa, por lo que el Worker alcanza al CP por el nombre del
servicio `control-plane` sin necesidad de exponer el puerto del CP al host.

Por eso el valor por defecto de `CONTROL_PLANE_URL` en el Worker es
`http://control-plane:8080`.

> Nota: el servicio `demo-app` del Compose del Worker usa un **bind mount**
> (`./demo-app-data`), no un volumen nombrado. El Worker detecta igualmente el
> compose project y sus rutas, pero tenlo en cuenta al añadir tus propios
> servicios: los bind mounts y los volúmenes nombrados se reportan de forma
> distinta en el inventario.

## Arranque

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

## Verificación rápida

Salud del Control Plane:

```powershell
curl http://127.0.0.1:18080/healthz
```

Primer login:

- usuario: `admin`
- contraseña inicial: `changeme`
- el sistema obliga a cambiarla en el primer acceso

```powershell
curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

Listar workers una vez cambiada la contraseña:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

## Primeros pasos después del arranque

Tras levantar el Control Plane y el Worker y hacer el primer login:

1. **Cambiar la contraseña** del usuario `admin` (obligatorio en el primer
   acceso).
2. **Crear un secreto** `rclone.conf` (tipo `file`) con la configuración del
   remoto donde se guardarán los backups. Opcionalmente un secreto de tipo
   `env` con la contraseña de Restic.
3. **Crear un storage profile** que referencie el secreto anterior y defina el
   `RESTIC_REPOSITORY` y la estrategia de backend (`restic-rclone`).
4. **Crear un target** seleccionando el worker y el compose project. Los
   `volume_targets` y `runtime_volumes` se derivan automáticamente del
   inventario del Worker; asocia el storage profile y, si quieres, una política
   de retención.
5. **Disparar un backup** desde el botón de la UI del target y revisar el job
   resultante en la vista de jobs.

## Uso con imágenes publicadas en GHCR

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Imagen esperada:

- `ghcr.io/danielrondongarcia/docker-volume-backup-control-plane`

## Enrolamiento seguro opcional

El stack de ejemplo arranca en HTTP para laboratorio rápido, pero el backend
soporta un modo seguro inspirado en el emparejamiento tipo Wazuh:

- `CONTROL_PLANE_TLS_ENABLED=true`: genera o reutiliza una CA local bajo
  `CONTROL_PLANE_TLS_DIR` y publica el CP por HTTPS.
- `CONTROL_PLANE_WORKER_MTLS_REQUIRED=true`: obliga a que las rutas operativas
  del worker usen el certificado cliente emitido por el CP.
- `POST /api/v1/admin/worker-enrollments`: crea un token de un solo uso,
  asigna `worker_id`, devuelve `ca_certificate_pem` y la huella del certificado
  servidor.
- `WORKER_ENROLLMENT_TOKEN`: habilita auto-enrolamiento del worker en el
  arranque.
- `WORKER_ENROLLMENT_CA_PEM`: permite hacer bootstrap de la confianza del
  worker antes de que exista su certificado cliente.
- `WORKER_TLS_DIR`, `WORKER_TLS_CA_FILE`, `WORKER_TLS_CERT_FILE`,
  `WORKER_TLS_KEY_FILE`: controlan dónde persistir la identidad TLS local del
  worker.

Flujo resumido:

1. Un admin crea el enrolamiento desde el Control Plane.
2. El operador entrega al worker el token y el PEM de la CA.
3. El worker genera su keypair local, envía CSR a
   `POST /api/v1/worker-enrollments/sign` y guarda `ca/cert/key`.
4. A partir de ahí, `heartbeat`, `inventory` y fetch/update de jobs usan mTLS
   con la huella persistida en el worker del CP.

## Apagado

```powershell
docker compose -f deploy/control-plane/docker-compose.yml down
```

## Notas

- Si `8080` ya está ocupado en el host, publica el stack en otro puerto usando
  `CONTROL_PLANE_PUBLISHED_PORT`.
- Para que el worker se registre, despliégalo desde `deploy/worker/`. Por
  defecto se conecta a la red `docker-volume-backup-control-plane_default` de
  este stack y apunta `CONTROL_PLANE_URL` a `http://control-plane:8080`.
- Este despliegue está orientado a validación local y laboratorio; `mTLS` ya
  existe de forma opcional, pero el Compose de referencia no lo activa por
  defecto.