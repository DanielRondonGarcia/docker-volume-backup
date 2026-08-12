# Control Plane Quickstart

## Objetivo

Esta guía permite probar la implementación inicial de `control_plane` + `worker_agent` con persistencia SQLite y ejecución de jobs básicos.

## Arranque del Control Plane

Por defecto, el Control Plane usa SQLite en `control_plane.db`.

```bash
python -m src.control_plane.main
```

Si ya tenías un proceso previo del Control Plane escuchando en `8080`, reinícialo antes de validar `/login` y `/api/v1/auth/*`, porque un proceso levantado antes de estos cambios seguirá sirviendo el router anterior hasta ser recreado.

Para validaciones limpias también puedes arrancarlo en otro puerto:

```bash
$env:CONTROL_PLANE_PORT="8091"
python -m src.control_plane.main
```

Variables útiles:

- `CONTROL_PLANE_HOST`
- `CONTROL_PLANE_PORT`
- `CONTROL_PLANE_REPOSITORY=sqlite|memory`
- `CONTROL_PLANE_DB_PATH`
- `CONTROL_PLANE_KEY_FILE`
- `CONTROL_PLANE_MASTER_KEY`
- `CONTROL_PLANE_USERS_JSON`
- `CONTROL_PLANE_USERS_FILE`
- `CONTROL_PLANE_SESSION_KEY`
- `CONTROL_PLANE_SESSION_KEY_FILE`
- `CONTROL_PLANE_SESSION_TTL_SECONDS`

## Autenticación y RBAC

La UI y los endpoints humanos ahora exigen autenticación.

Si no defines `CONTROL_PLANE_USERS_JSON`, el Control Plane crea un archivo local `.control_plane.users.json` con un usuario bootstrap:

- usuario: `admin`
- contraseña inicial: `changeme`
- rol: `admin`
- cambio de contraseña: obligatorio en el primer login

Roles soportados:

- `admin`
- `operator`
- `viewer`

Configuración recomendada por variable de entorno:

```bash
$env:CONTROL_PLANE_USERS_JSON='[
  {"username":"admin","password":"admin-seguro","role":"admin"},
  {"username":"operator1","password":"operator-seguro","role":"operator"},
  {"username":"viewer1","password":"viewer-seguro","role":"viewer"}
]'
```

Uso recomendado: dejar que el usuario bootstrap exista solo para el arranque inicial y reemplazar la contraseña inmediatamente al primer acceso.

Ejemplo mínimo para obtener una cookie de sesión y reutilizarla en pruebas manuales:

```bash
curl -c cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/login ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/auth/me
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/change-password ^
  -H "Content-Type: application/json" ^
  -d "{\"current_password\":\"changeme\",\"new_password\":\"AdminSeguro123\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/workers
```

## Acceso a la UI

Una vez levantado el Control Plane, la UI basica queda disponible en:

- `http://127.0.0.1:8080/`
- `http://127.0.0.1:8080/ui`

La UI actual permite:

- ver workers,
- ver targets,
- ver jobs,
- administrar usuarios locales del control plane cuando la autenticación usa archivo,
- crear secretos, storage profiles, políticas de retención y targets desde la misma UI,
- ver validacion, snapshots y stats de un target,
- disparar backup,
- disparar sync de snapshots,
- disparar sync de stats,
- disparar retencion,
- disparar restore dry-run.

Permisos por rol en esta fase:

- `viewer`: solo lectura
- `operator`: lectura + acciones operativas
- `admin`: lectura + acciones operativas + administración

## Administración local de usuarios

Cuando el control plane usa `CONTROL_PLANE_USERS_FILE` o el archivo bootstrap `.control_plane.users.json`, el rol `admin` puede gestionar usuarios locales desde la UI o la API.

Capacidades actuales:

- listar usuarios locales,
- crear usuarios locales,
- reasignar rol,
- marcar o desmarcar cambio obligatorio de contraseña,
- resetear contraseña y forzar nuevo cambio en el siguiente login.

Si en cambio defines `CONTROL_PLANE_USERS_JSON`, la autenticación queda respaldada por variables de entorno y la administración local se muestra en modo solo lectura para dejar claro que no hay persistencia editable desde la UI.

Listar usuarios:

```bash
curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/admin/users
```

Crear usuario:

```bash
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"operator1\",\"password\":\"OperatorTemp123\",\"role\":\"operator\",\"must_change_password\":true}"
```

Actualizar rol o bandera de cambio obligatorio:

```bash
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/update ^
  -H "Content-Type: application/json" ^
  -d "{\"role\":\"viewer\",\"must_change_password\":true}"
```

Resetear contraseña temporal:

```bash
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/reset-password ^
  -H "Content-Type: application/json" ^
  -d "{\"new_password\":\"ViewerTemp123\",\"must_change_password\":true,\"role\":\"viewer\"}"
```

## Formularios administrativos de recursos

Con rol `admin`, la UI ahora también expone formularios para:

- crear secretos,
- crear storage profiles,
- crear políticas de retención,
- crear targets nuevos.

Notas prácticas:

- los campos avanzados aceptan `JSON` válido,
- `volume_targets` admite una ruta por línea o separadas por comas,
- el formulario de targets usa los workers, storage profiles y políticas ya registrados para poblar sus selectores,
- los secretos listados en UI siguen mostrando solo metadatos públicos; el valor cifrado nunca se devuelve.

Si un formulario falla por un `JSON` inválido, la UI lo notifica antes de enviar una carga inconsistente al backend.

## Despliegue Docker de referencia

El repositorio separa el despliegue del Control Plane y del Worker en dos
composes distintos:

- `deploy/control-plane/docker-compose.yml` y `deploy/control-plane/docker-compose.ghcr.yml`:
  solo el Control Plane.
- `deploy/worker/docker-compose.yml` y `deploy/worker/docker-compose.ghcr.yml`:
  el Worker Agent junto con los servicios que se quieren backupear.

El worker se ejecuta en el mismo compose project que los servicios a backupear,
para que detecte automaticamente el proyecto, sus volumenes y reporte todo al
Control Plane. No se requiere `network_mode` porque el backup es solo de
archivos.

### Arrancar el Control Plane

```bash
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

Validación mínima del CP:

```bash
curl http://127.0.0.1:18080/healthz

curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

### Arrancar el Worker + servicios a backupear

Edita `deploy/worker/docker-compose.yml` para anadir tus servicios con sus
volumenes (el archivo incluye un servicio `demo-app` de ejemplo). Luego:

```bash
$env:CONTROL_PLANE_URL="http://host.docker.internal:18080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Si el Control Plane esta en otro host o puerto, ajusta `CONTROL_PLANE_URL`.

Verificación del worker:

```bash
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

Confirmar que el worker se registro en el CP:

```bash
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

Consultar el inventario reportado por el worker (incluye este compose project
con sus volumenes):

```bash
# $WORKER_ID se obtiene del endpoint /api/v1/workers
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory
```

### Consumir imágenes publicadas en GHCR

Control Plane:

```bash
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Worker + servicios:

```bash
$env:CONTROL_PLANE_URL="http://host.docker.internal:18080"
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

### Detener los stacks

Control Plane:

```bash
docker compose -f deploy/control-plane/docker-compose.yml down
```

Worker + servicios:

```bash
docker compose -f deploy/worker/docker-compose.yml down
```

## Arranque del Worker

```bash
$env:CONTROL_PLANE_URL="http://127.0.0.1:8080"
$env:WORKER_RUN_ONCE="false"
$env:WORKER_POLL_INTERVAL_SECONDS="15"
$env:WORKER_HEALTH_PORT="8081"
python -m src.worker_agent.main
```

Variables útiles:

- `CONTROL_PLANE_URL`
- `WORKER_NAME`
- `WORKER_HOST_NAME`
- `WORKER_VERSION`
- `WORKER_ID`
- `WORKER_LABELS`
- `BACKUP_RUNTIME_IMAGE`
- `WORKER_RUN_ONCE`
- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_HEALTH_HOST`
- `WORKER_HEALTH_PORT`

Variables adicionales para enrolamiento seguro:

- `CONTROL_PLANE_TLS_ENABLED`
- `CONTROL_PLANE_TLS_DIR`
- `CONTROL_PLANE_WORKER_MTLS_REQUIRED`
- `WORKER_ENROLLMENT_TOKEN`
- `WORKER_ENROLLMENT_CA_PEM`
- `WORKER_TLS_DIR`
- `WORKER_TLS_CA_FILE`
- `WORKER_TLS_CERT_FILE`
- `WORKER_TLS_KEY_FILE`

El worker publica `GET /healthz` con un payload similar a:

```json
{
  "ok": true,
  "status": "ok",
  "registered": true,
  "control_plane_reachable": true,
  "worker_id": "...",
  "last_successful_control_plane_contact_at": "2026-08-11T18:00:00+00:00"
}
```

Interpretación:

- `ok=true`: el proceso del worker está vivo y el endpoint responde
- `control_plane_reachable=true|false`: indica si el último intento de contacto con el CP fue exitoso
- `status=degraded`: el worker sigue vivo pero la conectividad con el CP falló en el último ciclo

## Enrolamiento TLS opcional

Arranque seguro del Control Plane:

```bash
$env:CONTROL_PLANE_PORT="18443"
$env:CONTROL_PLANE_TLS_ENABLED="true"
$env:CONTROL_PLANE_WORKER_MTLS_REQUIRED="true"
$env:CONTROL_PLANE_TLS_DIR="tmp/control-plane-tls"
python -m src.control_plane.main
```

Crear un enrolamiento desde una sesión admin ya autenticada:

```bash
curl -b cp-cookie.txt -X POST https://127.0.0.1:18443/api/v1/admin/worker-enrollments ^
  --cacert tmp/control-plane-tls/ca-cert.pem ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"worker-lab\",\"host_name\":\"host-lab\",\"ttl_minutes\":30,\"labels\":{\"env\":\"lab\"}}"
```

La respuesta devuelve:

- `worker_id`
- `token`
- `ca_certificate_pem`
- `server_certificate_fingerprint`

Arranque del worker con auto-enrolamiento:

```bash
$env:CONTROL_PLANE_URL="https://127.0.0.1:18443"
$env:WORKER_ENROLLMENT_TOKEN="<token>"
$env:WORKER_ENROLLMENT_CA_PEM=(Get-Content -Raw "tmp/control-plane-tls/ca-cert.pem")
$env:WORKER_TLS_DIR="tmp/worker-tls"
$env:WORKER_HEALTH_PORT="8081"
python -m src.worker_agent.main
```

Comportamiento esperado:

- si el worker no tiene identidad TLS local, genera su llave privada y CSR,
- el CP firma el certificado cliente y liga su huella al `worker_id`,
- el worker guarda `ca-cert.pem`, `worker-cert.pem` y `worker-key.pem` en `WORKER_TLS_DIR`,
- los siguientes `heartbeat`, `inventory` y operaciones de jobs usan mTLS.

## Flujo mínimo

1. Arrancar el Control Plane.
2. Arrancar el Worker.
3. Confirmar workers registrados:

```bash
curl http://127.0.0.1:8080/api/v1/workers
```

4. Consultar inventario:

```bash
curl http://127.0.0.1:8080/api/v1/workers/<worker_id>/inventory
```

## Crear un target de backup

Ejemplo orientado al runtime actual del proyecto:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"compose_project\":\"grafana\",\"volume_targets\":[\"/backup/grafana-data\"],\"backup_mode\":\"hot\",\"backup_strategy\":\"restic\",\"runtime_image\":\"ghcr.io/danielrondongarcia/docker-volume-backup\",\"runtime_environment\":{\"BACKUP_STRATEGY\":\"restic\",\"RESTIC_REPOSITORY\":\"/repo\",\"RESTIC_PASSWORD\":\"change-me\",\"BACKUP_SOURCES\":\"/backup/grafana-data\"},\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"},\"restic-repo\":{\"bind\":\"/repo\",\"mode\":\"rw\"}}}"
```

## Crear secretos y storage profiles

Crear un secreto:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/secrets ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"restic-password\",\"scope\":\"global\",\"secret_type\":\"env\",\"plaintext\":\"change-me\"}"
```

Crear un secreto de archivo para `rclone.conf`:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/secrets ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"rclone-config\",\"scope\":\"global\",\"secret_type\":\"file\",\"plaintext\":\"[remote]\\ntype = s3\\nprovider = AWS\"}"
```

Crear un `storage profile` usando referencias a secretos:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/storage-profiles ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"s3-restic\",\"backend_type\":\"restic-rclone\",\"environment\":{\"RESTIC_REPOSITORY\":\"rclone:remote:/docker-backups\",\"RCLONE_CONFIG\":\"/run/secrets/rclone.conf\"},\"secret_refs\":{\"RESTIC_PASSWORD\":\"<secret_id_password>\"},\"file_secret_refs\":{\"/run/secrets/rclone.conf\":\"<secret_id_rclone_conf>\"}}"
```

Listar `storage profiles`:

```bash
curl http://127.0.0.1:8080/api/v1/storage-profiles
```

Listar secretos:

```bash
curl http://127.0.0.1:8080/api/v1/secrets
```

## Crear política de retención

```bash
curl -X POST http://127.0.0.1:8080/api/v1/retention-policies ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"default-retention\",\"keep_daily\":7,\"keep_weekly\":4,\"keep_monthly\":12,\"keep_yearly\":1,\"prune\":true}"
```

Listar políticas:

```bash
curl http://127.0.0.1:8080/api/v1/retention-policies
```

### Asociar el target a un storage profile y política de retención

Al crear el target, enviar `storage_profile_id` y opcionalmente `retention_policy_id`:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"storage_profile_id\":\"<profile_id>\",\"retention_policy_id\":\"<policy_id>\",\"volume_targets\":[\"/backup/grafana-data\"],\"runtime_image\":\"ghcr.io/danielrondongarcia/docker-volume-backup\",\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"}}}"
```

## Disparar backup para un target

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/backup ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\"}"
```

## Sincronizar snapshots de un target

Esto encola un job para ejecutar `restic snapshots --json` en el worker y persistir el catálogo en el Control Plane.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots-sync ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\"}"
```

Consultar snapshots catalogados:

```bash
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots
```

## Sincronizar stats del target

Esto encola un job para ejecutar `restic stats --mode raw-data --json`.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/stats-sync ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\"}"
```

Consultar stats persistidos:

```bash
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/stats
```

## Ejecutar retención

Esto encola un job para ejecutar `restic forget` con la política configurada para el target.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/retention-run ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\"}"
```

## Restore remoto

Dry-run:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/dry-run ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"layout\":\"auto\",\"stop_containers\":true}"
```

Restore real:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/run ^
  -H "Content-Type: application/json" ^
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"force_overwrite\":true,\"stop_containers\":true}"
```

## Validar configuración de un target

La validación actual revisa configuración mínima del runtime, Restic, storage profile y montaje esperado para restore.

```bash
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/validate
```

## Encolar un job genérico al worker

Self-check:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs ^
  -H "Content-Type: application/json" ^
  -d "{\"command\":\"worker.self_check\",\"requested_by\":\"admin\"}"
```

Refrescar inventario:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs ^
  -H "Content-Type: application/json" ^
  -d "{\"command\":\"inventory.refresh\",\"requested_by\":\"admin\"}"
```

## Consultar jobs

```bash
curl http://127.0.0.1:8080/api/v1/jobs
```

## Estado actual

Lo ya implementado:

- persistencia SQLite para workers, inventory, targets y jobs,
- storage profiles persistidos,
- secretos cifrados en el control plane,
- registro y heartbeat,
- inventario Docker,
- creación de targets con `runtime_environment` y `runtime_volumes`,
- resolución de secretos a variables de entorno,
- materialización efímera de archivos sensibles en el worker,
- catálogo persistido de snapshots por target,
- stats persistidos por target,
- políticas de retención persistidas,
- dispatch de restore remoto en dry-run y ejecución real,
- dispatch de stats y retención,
- validación de configuración por target,
- dispatch de backup,
- jobs genéricos para el worker,
- polling continuo configurable del agent,
- autenticación local con sesiones firmadas,
- cambio obligatorio de contraseña en primer login,
- administración básica de usuarios locales desde UI y API.

Lo pendiente:

- mTLS,
- restore guiado con validaciones avanzadas y aprobaciones,
- validación post-restore,
- SSO real (`OIDC`/`SAML`),
- formularios administrativos para el resto de recursos del control plane.
