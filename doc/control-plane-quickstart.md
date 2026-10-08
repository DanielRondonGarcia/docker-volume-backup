# Control Plane — Guía completa

Esta guía cubre el despliegue y la operación de `control_plane` + `worker_agent`
con persistencia SQLite. Para un inicio rápido en 3 comandos, consulta el
[README principal](../README.md).

## 0. Reset de la release mayor

No hay migración: elimina los volúmenes anteriores y configura desde cero;
conserva el `WORKER_ID` explícito.

## 1. Despliegue con Docker

El repositorio separa el despliegue del Control Plane y del Worker en dos
Composes distintos:

- `deploy/control-plane/docker-compose.yml` (y `docker-compose.ghcr.yml`):
  solo el Control Plane.
- `deploy/worker/docker-compose.yml` (y `docker-compose.ghcr.yml`):
  el Worker Agent junto con los servicios que se quieren backupear.

El Worker se ejecuta en el mismo Compose project que los servicios a backupear,
para que detecte automáticamente el proyecto, sus volúmenes y reporte todo al
Control Plane. No se requiere `network_mode` porque el backup es solo de
archivos.

### Conexión entre stacks (red compartida)

El Compose del Worker se conecta por defecto a la red externa
`docker-volume-backup-control-plane_default` (la red que crea el Compose del
Control Plane). Por eso `CONTROL_PLANE_URL` por defecto es
`http://control-plane:8080` y el Worker alcanza al CP por el nombre del servicio
sin necesidad de exponer el puerto del CP al host.

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL` al
`host:puerto` publicado del CP.

El Compose del Control Plane incluye el servicio interno `redis` (Redis 7 con
AOF y volumen `snapshot_explorer_redis`). El Worker usa por defecto
`redis://redis:6379/0` para cachear únicamente metadatos acotados de Snapshot
Explorer; Redis no se publica al host. El TTL por defecto es de 86400 segundos
(24 horas), con un maximo de 86400 y hasta 1000 entradas por target y
repositorio. El servicio usa `maxmemory 128mb` con `allkeys-lru`, por lo que la
persistencia AOF no evita la expiracion ni la eviccion. La cache es opcional: si
Redis falta, no responde o se configura mal, el Worker vuelve a Restic y el
catalogo del CP continua respaldado por SQLite.

Para desactivarla de forma explícita, define una URL vacía antes de levantar el
Worker:

```powershell
$env:SNAPSHOT_EXPLORER_REDIS_URL=""
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

### Arrancar el Control Plane

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

Validación mínima del CP:

```powershell
curl http://127.0.0.1:18080/healthz

curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

### Arrancar el Worker + servicios a backupear

Genera en la UI una credencial HMAC, guarda el secreto y expón
`WORKER_ENROLLMENT_TOKEN` solo una vez; el worker lo guarda (`0600`).

Edita `deploy/worker/docker-compose.yml` para añadir tus servicios con sus
volúmenes (el archivo incluye un servicio `demo-app` de ejemplo). Luego:

```powershell
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL`:

```powershell
$env:CONTROL_PLANE_URL="http://192.168.1.10:18080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Verificación del worker:

```powershell
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

Confirmar que el worker se registró en el CP:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

Consultar el inventario reportado por el worker (incluye este Compose project
con sus volúmenes):

```powershell
# $WORKER_ID se obtiene del endpoint /api/v1/workers
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory
```

### Consumir imágenes publicadas en GHCR

Control Plane:

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Worker + servicios:

```powershell
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

Imágenes esperadas:

- `ghcr.io/danielrondongarcia/docker-volume-backup-control-plane`
- `ghcr.io/danielrondongarcia/docker-volume-backup-worker`

### Detener los stacks

```powershell
docker compose -f deploy/control-plane/docker-compose.yml down
docker compose -f deploy/worker/docker-compose.yml down
```

## 2. Despliegue nativo (sin Docker)

Por defecto, el Control Plane usa SQLite en `control_plane.db`.

```powershell
python -m src.control_plane.main
```

Si ya tenías un proceso previo del Control Plane escuchando en `8080`,
reinícialo antes de validar `/login` y `/api/v1/auth/*`, porque un proceso
levantado antes de estos cambios seguirá sirviendo el router anterior hasta ser
recreado.

Para validaciones limpias también puedes arrancarlo en otro puerto:

```powershell
$env:CONTROL_PLANE_PORT="8091"
python -m src.control_plane.main
```

Variables útiles del Control Plane:

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
- `CONTROL_PLANE_PUBLIC_URL`: URL pública que la UI usa al generar los comandos de despliegue del worker. Úsala cuando el Control Plane esté detrás de un proxy, en un puerto distinto, o con un dominio. Ejemplo: `https://backups.miempresa.com`. Si no se define, la UI usa la URL del navegador (`window.location`).

Arranque del Worker en modo nativo:

```powershell
$env:CONTROL_PLANE_URL="http://127.0.0.1:8080"
$env:WORKER_RUN_ONCE="false"
$env:WORKER_POLL_INTERVAL_SECONDS="15"
$env:WORKER_HEALTH_PORT="8081"
python -m src.worker_agent.main
```

Variables útiles del Worker:

- `CONTROL_PLANE_URL`
- `WORKER_NAME`
- `WORKER_HOST_NAME`
- `WORKER_VERSION`
- `WORKER_ID`
- `WORKER_LABELS`
- `BACKUP_RUNTIME_IMAGE`
- `WORKER_RUNTIME=docker|kubernetes|native`
- `WORKER_RUN_ONCE`
- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_HEALTH_HOST`
- `WORKER_HEALTH_PORT`
- `WORKER_CREDENTIAL_FILE`
- `CONTROL_PLANE_CA_FILE`

### Worker nativo para filesystem

El wrapper recomendado para respaldos de filesystem nativo es:

```powershell
python -m src.worker_agent.cli daemon
```

Ese comando corre en primer plano, selecciona `WORKER_RUNTIME=native` y usa
polling continuo por defecto (`WORKER_RUN_ONCE=false`). Para depuración manual y
sin instalar un runner del SO puedes ejecutar una sola iteración explícita:

```powershell
python -m src.worker_agent.cli daemon --once
```

Diagnóstico local redacted:

```powershell
python -m src.worker_agent.cli self-check
```

El self-check informa presencia de configuración y disponibilidad de ejecutables
sin imprimir valores de entorno, URL, tokens, credenciales, firmas ni contenido
del archivo de credenciales. Los ejecutables externos (`restic`, `rclone`,
`gpg`, `aws`, `ssh`, `scp`, `tar`) se instalan manualmente en el host; no todos
son obligatorios para todos los targets, dependen del storage profile y de las
operaciones habilitadas.

Configuración recomendada:

- guarda `CONTROL_PLANE_URL`, `WORKER_NAME`, `WORKER_CREDENTIAL_FILE` y otros
  valores no secretos en un archivo de entorno protegido;
- expón `WORKER_ENROLLMENT_TOKEN` solo durante el primer registro: es de un solo
  uso y debe retirarse del archivo de arranque después de que el worker guarde
  sus credenciales;
- no escribas tokens, contraseñas, secretos Restic/Rclone ni contenido de
  credenciales en unidades systemd, tareas programadas, scripts compartidos o
  logs;
- protege el archivo de credenciales, el archivo de entorno y el estado local
  con permisos restrictivos (`chmod 600` y dueño del usuario del worker en
  Linux; ACLs equivalentes en Windows);
- concede al usuario del worker lectura en cada `filesystem_path` de origen y
  escritura solo donde el flujo lo requiera (por ejemplo repositorio local,
  cache, restore o estado del worker).

#### Runner Linux con systemd

El repositorio incluye una unidad de ejemplo en
`deploy/worker/native/docker-volume-backup-worker.service`. Cópiala a
`/etc/systemd/system/`, ajusta `User`, `Group`, `WorkingDirectory`,
`EnvironmentFile` y `ReadWritePaths`, y crea el archivo de entorno fuera del
repositorio, por ejemplo `/etc/docker-volume-backup/worker.env`. La unidad deja
escribible `/var/lib/docker-volume-backup` para credenciales y estado del worker;
si cambias `WORKER_CREDENTIAL_FILE` o añades cache/estado local, mantenlos dentro
de esa ruta o añade una ruta de estado igualmente acotada en `ReadWritePaths`:

```ini
CONTROL_PLANE_URL=https://backups.example.com
WORKER_NAME=worker-linux-01
WORKER_CREDENTIAL_FILE=/var/lib/docker-volume-backup/worker_credentials.json
WORKER_HEALTH_PORT=8081
```

No incluyas secretos permanentes en la unidad. Si necesitas enrolar el worker,
añade temporalmente `WORKER_ENROLLMENT_TOKEN`, arranca el servicio una vez,
confirma el registro y elimina esa línea antes de dejar el servicio en modo
continuo.

El sandbox de systemd usa `ProtectSystem=full` y `ProtectHome=read-only`: el
usuario del worker debe conservar lectura sobre cada `filesystem_path` de origen,
pero solo puede escribir en las rutas listadas en `ReadWritePaths`. Para restore
a destinos bloqueados por `ProtectHome` o `ProtectSystem`, añade únicamente el
destino aprobado (por ejemplo `/srv/app-restore`) a `ReadWritePaths`; no
deshabilites el sandbox globalmente por defecto.

Comandos típicos:

```bash
sudo install -d -m 0700 -o dvb-worker -g dvb-worker /var/lib/docker-volume-backup
sudo install -d -m 0750 /etc/docker-volume-backup
sudo systemctl daemon-reload
sudo systemctl enable --now docker-volume-backup-worker.service
sudo systemctl status docker-volume-backup-worker.service
journalctl -u docker-volume-backup-worker.service -n 50 --no-pager
```

#### Runner Windows MVP con Task Scheduler

El MVP de Windows no instala un servicio nativo de Windows SCM y no requiere
wrappers de terceros. Usa una tarea de inicio de Windows Task Scheduler que lance
el módulo de Python en primer plano:

```powershell
$WorkerAccount = "DOMAIN\dvb-worker"
$PythonExe = "C:\Python312\python.exe"          # Ajusta a la ruta real de Python.
$WorkingDirectory = "C:\docker-volume-backup"  # Ajusta a la copia local del repo/app.
$Credential = Get-Credential -UserName $WorkerAccount -Message "Credencial de la cuenta dedicada del worker"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m src.worker_agent.cli daemon" -WorkingDirectory $WorkingDirectory
$Trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "DockerVolumeBackupWorker" -Action $Action -Trigger $Trigger -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited
Remove-Variable Credential
```

Ejecuta el registro desde PowerShell elevado; el prompt recoge la contraseña sin
escribirla en el archivo ni en el argumento de la tarea. La cuenta debe tener
permiso de inicio de sesión por lotes y acceso a las rutas configuradas.

Define variables de entorno no secretas en el perfil del usuario de la tarea o
en un mecanismo administrado por el operador. No insertes tokens ni contraseñas
en el argumento de la tarea. Aplica ACLs restrictivas al directorio de estado,
al archivo de credenciales y a cualquier archivo de configuración usado por el
worker; el usuario de la tarea también debe poder leer los paths origen.

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

- `ok=true`: el proceso del worker está vivo y el endpoint responde.
- `control_plane_reachable=true|false`: indica si el último intento de contacto
  con el CP fue exitoso.
- `status=degraded`: el worker sigue vivo pero la conectividad con el CP falló
  en el último ciclo.

Flujo mínimo nativo:

1. Arrancar el Control Plane.
2. Arrancar el Worker.
3. Confirmar workers registrados:

```powershell
curl http://127.0.0.1:8080/api/v1/workers
```

4. Consultar inventario:

```powershell
curl http://127.0.0.1:8080/api/v1/workers/<worker_id>/inventory
```

## 3. Autenticación y RBAC

La UI y los endpoints humanos exigen autenticación.

Si no defines `CONTROL_PLANE_USERS_JSON`, el Control Plane crea un archivo local
`.control_plane.users.json` con un usuario bootstrap:

- usuario: `admin`
- contraseña inicial: `changeme`
- rol: `admin`
- cambio de contraseña: obligatorio en el primer login

Roles soportados:

- `admin`
- `operator`
- `viewer`

Permisos por rol en esta fase:

- `viewer`: solo lectura.
- `operator`: lectura + acciones operativas.
- `admin`: lectura + acciones operativas + administración.

Configuración recomendada por variable de entorno:

```powershell
$env:CONTROL_PLANE_USERS_JSON='[
  {"username":"admin","password":"admin-seguro","role":"admin"},
  {"username":"operator1","password":"operator-seguro","role":"operator"},
  {"username":"viewer1","password":"viewer-seguro","role":"viewer"}
]'
```

Uso recomendado: dejar que el usuario bootstrap exista solo para el arranque
inicial y reemplazar la contraseña inmediatamente al primer acceso.

Ejemplo mínimo para obtener una cookie de sesión y reutilizarla en pruebas
manuales:

```powershell
curl -c cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/auth/me

curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/change-password `
  -H "Content-Type: application/json" `
  -d "{\"current_password\":\"changeme\",\"new_password\":\"AdminSeguro123\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/workers
```

### Administración local de usuarios

Cuando el Control Plane usa `CONTROL_PLANE_USERS_FILE` o el archivo bootstrap
`.control_plane.users.json`, el rol `admin` puede gestionar usuarios locales
desde la UI o la API.

Capacidades actuales:

- listar usuarios locales,
- crear usuarios locales,
- reasignar rol,
- marcar o desmarcar cambio obligatorio de contraseña,
- resetear contraseña y forzar nuevo cambio en el siguiente login.

Si en cambio defines `CONTROL_PLANE_USERS_JSON`, la autenticación queda
respaldada por variables de entorno y la administración local se muestra en modo
solo lectura para dejar claro que no hay persistencia editable desde la UI.

Listar usuarios:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/admin/users
```

Crear usuario:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"operator1\",\"password\":\"OperatorTemp123\",\"role\":\"operator\",\"must_change_password\":true}"
```

Actualizar rol o bandera de cambio obligatorio:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/update `
  -H "Content-Type: application/json" `
  -d "{\"role\":\"viewer\",\"must_change_password\":true}"
```

Resetear contraseña temporal:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/reset-password `
  -H "Content-Type: application/json" `
  -d "{\"new_password\":\"ViewerTemp123\",\"must_change_password\":true,\"role\":\"viewer\"}"
```

## 4. UI

Una vez levantado el Control Plane, la UI queda disponible en:

- `http://127.0.0.1:8080/`
- `http://127.0.0.1:8080/ui`

La UI actual permite:

- ver workers,
- ver targets,
- ver jobs,
- administrar usuarios locales del Control Plane cuando la autenticación usa
  archivo,
- crear secretos, storage profiles, políticas de retención y targets desde la
  misma UI,
- ver validación, snapshots y stats de un target,
- disparar backup,
- disparar sync de snapshots,
- disparar sync de stats,
- disparar retención,
- disparar restore dry-run.

Con rol `admin`, la UI también expone formularios administrativos para crear
secretos, storage profiles, políticas de retención y targets nuevos.

Notas prácticas de los formularios:

- los campos avanzados aceptan `JSON` válido,
- `volume_targets` admite una ruta por línea o separadas por comas,
- el formulario de targets usa los workers, storage profiles y políticas ya
  registrados para poblar sus selectores,
- los secretos listados en UI muestran solo metadatos públicos; el valor
  cifrado nunca se devuelve,
- si un formulario falla por un `JSON` inválido, la UI lo notifica antes de
  enviar una carga inconsistente al backend.

## 5. Configuración de backup

### Crear un target de backup

Ejemplo orientado al runtime actual del proyecto:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"compose_project\":\"grafana\",\"volume_targets\":[\"/backup/grafana-data\"],\"backup_mode\":\"hot\",\"backup_strategy\":\"restic\",\"runtime_image\":\"ghcr.io/danielrondongarcia/docker-volume-backup\",\"runtime_environment\":{\"BACKUP_STRATEGY\":\"restic\",\"RESTIC_REPOSITORY\":\"/repo\",\"RESTIC_PASSWORD\":\"change-me\",\"BACKUP_SOURCES\":\"/backup/grafana-data\"},\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"},\"restic-repo\":{\"bind\":\"/repo\",\"mode\":\"rw\"}}}"
```

### Crear secretos y storage profiles

Crear un secreto:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/secrets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"restic-password\",\"scope\":\"global\",\"secret_type\":\"env\",\"plaintext\":\"change-me\"}"
```

Crear un secreto de archivo para `rclone.conf`:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/secrets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"rclone-config\",\"scope\":\"global\",\"secret_type\":\"file\",\"plaintext\":\"[remote]\\ntype = s3\\nprovider = AWS\"}"
```

Crear un `storage profile` usando referencias a secretos:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/storage-profiles `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"s3-restic\",\"backend_type\":\"restic-rclone\",\"environment\":{\"RESTIC_REPOSITORY\":\"rclone:remote:/docker-backups\",\"RCLONE_CONFIG\":\"/run/secrets/rclone.conf\"},\"secret_refs\":{\"RESTIC_PASSWORD\":\"<secret_id_password>\"},\"file_secret_refs\":{\"/run/secrets/rclone.conf\":\"<secret_id_rclone_conf>\"}}"
```

Listar `storage profiles`:

```powershell
curl http://127.0.0.1:8080/api/v1/storage-profiles
```

Listar secretos:

```powershell
curl http://127.0.0.1:8080/api/v1/secrets
```

### Crear política de retención

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/retention-policies `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"default-retention\",\"keep_daily\":7,\"keep_weekly\":4,\"keep_monthly\":12,\"keep_yearly\":1,\"prune\":true}"
```

Listar políticas:

```powershell
curl http://127.0.0.1:8080/api/v1/retention-policies
```

### Asociar el target a un storage profile y política de retención

Al crear el target, enviar `storage_profile_id` y opcionalmente
`retention_policy_id`:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"storage_profile_id\":\"<profile_id>\",\"retention_policy_id\":\"<policy_id>\",\"volume_targets\":[\"/backup/grafana-data\"],\"runtime_image\":\"ghcr.io/danielrondongarcia/docker-volume-backup\",\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"}}}"
```

### Validar configuración de un target

La validación revisa configuración mínima del runtime, Restic, storage profile
y montaje esperado para restore.

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/validate
```

## 6. Operaciones

### Disparar backup para un target

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/backup `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

### Sincronizar snapshots de un target

Encola un job para ejecutar `restic snapshots --json` en el worker y persistir
el catálogo en el Control Plane.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots-sync `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

Consultar snapshots catalogados:

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots
```

### Sincronizar stats del target

Encola un job para ejecutar `restic stats --mode raw-data --json`.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/stats-sync `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

Consultar stats persistidos:

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/stats
```

### Ejecutar retención

Encola un job para ejecutar `restic forget` con la política configurada para el
target.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/retention-run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

### Restore remoto

Dry-run:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/dry-run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"layout\":\"auto\",\"stop_containers\":true}"
```

Restore real:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"force_overwrite\":true,\"stop_containers\":true}"
```

### Encolar un job genérico al worker

Self-check:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs `
  -H "Content-Type: application/json" `
  -d "{\"command\":\"worker.self_check\",\"requested_by\":\"admin\"}"
```

Refrescar inventario:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs `
  -H "Content-Type: application/json" `
  -d "{\"command\":\"inventory.refresh\",\"requested_by\":\"admin\"}"
```

### Consultar jobs

```powershell
curl http://127.0.0.1:8080/api/v1/jobs
```

## 7. Enrolamiento HMAC y transporte

HMAC usa secreto de un solo uso, digest en CP y archivo `0600`; no hay
URLs/logs con secretos, registro abierto, certificados de cliente, CSR, huellas
ni fallback. Variables: `WORKER_ENROLLMENT_TOKEN`, `WORKER_CREDENTIAL_FILE`,
`WORKER_ID`, `CONTROL_PLANE_URL` y `CONTROL_PLANE_CA_FILE` para HTTPS server-only.
HTTP local no es confidencial; tras cinco fallos el worker termina.
