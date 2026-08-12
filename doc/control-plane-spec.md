# Control Plane y Worker Agent

## Objetivo

Diseñar una evolución de `docker-volume-backup` hacia una plataforma centralizada de respaldo y restauración, compuesta por:

- Un `Control Plane` con UI, API, scheduler, metadatos, secretos y auditoría.
- Uno o varios `Worker Agent` desplegados en hosts con Docker.
- Un runtime de ejecución reutilizable basado en la imagen actual de backup/restore del proyecto.

El sistema debe permitir operar backups y restores de forma centralizada usando principalmente `restic` + `rclone`, con soporte de backups en frío o en caliente, gestión de retención y operación remota segura.

## Alcance funcional

La primera línea de producto debe cubrir:

- Registro seguro de workers desde el Control Plane.
- Inventario remoto de hosts, contenedores, compose projects, volúmenes y mounts.
- Definición de `Backup Targets` centralizados.
- Gestión de secretos por target, por backend y por worker.
- Gestión de `rclone.conf` y perfiles lógicos por target.
- Políticas de retención parametrizables desde UI/API.
- Ejecución remota de:
  - backup on-demand,
  - backup programado,
  - `restic snapshots`,
  - `restic stats`,
  - `restic forget --prune`,
  - restore dry-run,
  - restore real,
  - stop/start/restart de contenedores implicados.
- Auditoría completa de acciones sensibles.
- UI con autenticación configurable: `basic`, `OIDC` o `SAML`.

## No objetivos iniciales

Para evitar un alcance riesgoso en la primera iteración, quedan fuera del MVP:

- Shell remoto arbitrario.
- Ejecución libre de comandos Docker desde UI.
- Acceso remoto directo del Control Plane a `docker.sock`.
- Multi-control-plane activo/activo.
- Gestión genérica de secretos empresariales externos como requisito obligatorio del MVP.
- Auto-remediación completa posterior a restore sin validación humana.

## Principios de diseño

1. El Control Plane no ejecuta operaciones Docker directamente en hosts remotos.
2. El `docker.sock` solo se monta en el `Worker Agent` local del host.
3. Las operaciones de backup/restore se ejecutan en jobs efímeros o controlados por el worker.
4. El runtime existente del repositorio se reutiliza como motor de ejecución.
5. Toda comunicación entre CP y Worker usa autenticación mutua obligatoria.
6. Ningún secreto de enrolamiento o material criptográfico sensible debe ocultarse en código fuente.
7. Toda acción destructiva requiere trazabilidad, expiración y validación explícita.
8. El modelo operativo debe soportar modo frío y modo caliente por target.

## Arquitectura lógica

### Componentes principales

#### 1. Control Plane

Servicio central con las siguientes responsabilidades:

- API de administración y operación.
- UI web.
- Scheduler central.
- Inventario y metadata.
- Gestión de secretos.
- Gestión de políticas.
- Emisión y rotación de certificados para workers.
- Auditoría.
- Coordinación de operaciones remotas.

#### 2. Worker Agent

Servicio residente por host Docker, con las siguientes responsabilidades:

- Mantener una conexión segura con el Control Plane.
- Reportar inventario y health.
- Resolver operaciones locales contra Docker.
- Lanzar jobs de backup/restore.
- Aplicar políticas operativas locales.
- Manejar secretos efímeros en memoria o archivos temporales.
- Aislar el acceso al `docker.sock`.

#### 3. Runtime de ejecución

Se reutiliza el runtime actual del repositorio:

- Imagen actual de `docker-volume-backup`.
- Modo backup.
- Modo restore.
- Estrategias `tar` y `restic`.
- Restore inteligente ya implementado.

El worker no debe reimplementar la lógica compleja de backup/restore si ya existe en el runtime actual.

#### 4. Base de datos de metadatos

Persistencia del Control Plane para:

- workers,
- hosts,
- backup targets,
- storage profiles,
- retention policies,
- jobs,
- snapshots indexados,
- auditoría,
- sesiones de enrolamiento,
- configuración de auth,
- secretos referenciados.

#### 5. Vault interno

Componente lógico o servicio interno del Control Plane para guardar:

- secretos cifrados por target,
- certificados emitidos,
- `RESTIC_PASSWORD`,
- credenciales cloud,
- configuración de `rclone`,
- bootstrap secrets,
- claves de firma interna.

#### 6. Identity Provider

Proveedor de identidad configurable:

- `basic` para despliegues simples o contingencia.
- `OIDC` preferido para integración con Entra ID.
- `SAML` cuando sea requerido por el entorno.

## Topología de despliegue

### En el Control Plane

- `cp-api`
- `cp-ui`
- `cp-scheduler`
- `cp-db`
- `cp-vault`

En despliegues pequeños, `cp-api`, `cp-ui` y `cp-scheduler` pueden convivir en un solo servicio lógico.

### En cada host administrado

- `worker-agent`
- acceso local a `docker.sock`
- jobs efímeros de backup/restore usando la imagen del proyecto

## Modelo de confianza

### Regla principal

El canal de control debe ser seguro incluso si la red subyacente no es de confianza.

### Reglas

- El worker debe autenticarse ante el CP mediante certificado cliente.
- El CP debe autenticarse ante el worker mediante certificado servidor.
- Cada orden crítica debe llevar:
  - identificador único,
  - timestamp,
  - expiración,
  - firma lógica del mensaje,
  - actor origen,
  - target destino.
- El worker debe rechazar órdenes expiradas o reusadas.

## Enrolamiento seguro de workers

### Decisión

No usar un `salt` oculto en código. Eso no es un control robusto.

### Diseño propuesto

Al bootstrap del Control Plane:

- se genera una CA interna,
- se genera un `bootstrap secret` aleatorio,
- se almacena en el vault interno,
- se habilita la emisión de tokens de enrolamiento de un solo uso.

### Flujo de enrolamiento

1. Un administrador crea un enrolamiento para un host o worker.
2. El CP genera:
   - `worker_enrollment_token`,
   - `worker_id`,
   - TTL corto,
   - huella del CP,
   - parámetros mínimos de conexión.
3. El operador arranca el worker con ese token.
4. El worker abre un canal TLS hacia el CP.
5. El worker presenta su token de enrolamiento.
6. El CP valida el token y responde con un desafío.
7. El worker genera una keypair local.
8. El worker envía CSR.
9. El CP firma el certificado cliente y devuelve:
   - certificado del worker,
   - cadena de confianza,
   - política base,
   - intervalo de rotación.
10. El worker invalida el token inicial.

### Implementación inicial aterrizada

La primera iteración implementa este flujo de forma pragmática:

- el CP genera y persiste una CA local `self-signed`,
- el admin crea `worker_enrollments` de un solo uso con `worker_id` preasignado,
- el token se persiste en hash, no en claro,
- el PEM de la CA se entrega al worker por canal administrativo fuera de banda,
- el worker genera su keypair local, envía CSR a `POST /api/v1/worker-enrollments/sign` y recibe su certificado firmado,
- el CP persiste la huella del certificado cliente en el `WorkerRecord`,
- cuando `CONTROL_PLANE_WORKER_MTLS_REQUIRED=true`, las rutas operativas del worker aceptan solo el certificado cuya huella coincide con ese `worker_id`.

La fase inicial no introduce todavía un desafío adicional separado entre token y CSR; el token de un solo uso y el canal TLS ya validado cubren el bootstrap básico.

### Material derivado

Si se requiere un hash de entorno para bootstrap, debe derivarse de:

- `worker_id`,
- `bootstrap secret`,
- token de un solo uso,
- `salt` aleatorio por worker,
- KDF robusta.

El `salt` debe persistirse como dato de configuración del worker o del enrolamiento, no como secreto hardcodeado.

## Modelo funcional

### Entidades principales

#### Worker

- `id`
- `name`
- `status`
- `version`
- `host_name`
- `docker_endpoint_mode`
- `last_seen_at`
- `certificate_serial`
- `labels`

#### Host Inventory

- `worker_id`
- `docker_info`
- `compose_projects`
- `containers`
- `volumes`
- `mounts`
- `networks`

#### Backup Target

Representa un activo administrable.

Campos sugeridos:

- `id`
- `name`
- `worker_id`
- `compose_project`
- `volume_targets[]`
- `backup_mode` = `hot|cold`
- `backup_strategy` = `restic|tar`
- `storage_profile_id`
- `retention_policy_id`
- `execution_policy_id`
- `restore_defaults`
- `labels`
- `enabled`

#### Storage Profile

Define el backend y credenciales lógicas.

- `id`
- `type` = `local|s3|scp|rclone|restic-repo`
- `restic_repository`
- `rclone_profile_name`
- `rclone_config_secret_ref`
- `credential_secret_refs[]`
- `extra_env`
- `verification_policy`

#### Secret Material

- `id`
- `scope` = `global|worker|target|storage-profile`
- `type`
- `ciphertext`
- `key_version`
- `metadata`

#### Retention Policy

- `id`
- `name`
- `keep_last`
- `keep_hourly`
- `keep_daily`
- `keep_weekly`
- `keep_monthly`
- `keep_yearly`
- `prune_after_backup`
- `check_after_prune`

#### Execution Policy

- `id`
- `name`
- `stop_containers`
- `restart_after_backup`
- `allow_hot_backup`
- `pre_commands[]`
- `post_commands[]`
- `timeout_seconds`
- `retry_policy`
- `concurrency_limit`

#### Backup Job

- `id`
- `target_id`
- `worker_id`
- `type` = `backup|restore|snapshot-list|stats|prune|stop|start|restart`
- `requested_by`
- `trigger` = `manual|schedule|policy|system`
- `status`
- `submitted_at`
- `started_at`
- `finished_at`
- `result_summary`
- `log_ref`
- `audit_ref`

#### Snapshot Catalog

Catálogo local de metadatos conocidos para acelerar UI.

- `id`
- `target_id`
- `snapshot_id`
- `created_at`
- `hostname`
- `paths`
- `size_bytes`
- `tags`
- `origin_worker_id`
- `last_verified_at`

## API del Control Plane

### Principios

- API versionada: `/api/v1`
- respuestas idempotentes cuando aplique
- todas las acciones sensibles generan evento de auditoría
- operaciones largas devuelven `job_id`

### Endpoints sugeridos

#### Auth

- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/providers`

#### Workers

- `POST /api/v1/workers/enrollments`
- `POST /api/v1/workers/register`
- `GET /api/v1/workers`
- `GET /api/v1/workers/{workerId}`
- `POST /api/v1/workers/{workerId}/rotate-certificate`
- `POST /api/v1/workers/{workerId}/disable`

#### Inventory

- `GET /api/v1/workers/{workerId}/inventory`
- `POST /api/v1/workers/{workerId}/inventory/refresh`

#### Backup Targets

- `GET /api/v1/targets`
- `POST /api/v1/targets`
- `GET /api/v1/targets/{targetId}`
- `PUT /api/v1/targets/{targetId}`
- `POST /api/v1/targets/{targetId}/enable`
- `POST /api/v1/targets/{targetId}/disable`

#### Execution

- `POST /api/v1/targets/{targetId}/backup`
- `POST /api/v1/targets/{targetId}/restore/dry-run`
- `POST /api/v1/targets/{targetId}/restore`
- `POST /api/v1/targets/{targetId}/snapshots/sync`
- `POST /api/v1/targets/{targetId}/retention/run`
- `POST /api/v1/targets/{targetId}/containers/stop`
- `POST /api/v1/targets/{targetId}/containers/start`
- `POST /api/v1/targets/{targetId}/containers/restart`

#### Jobs

- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{jobId}`
- `GET /api/v1/jobs/{jobId}/logs`
- `POST /api/v1/jobs/{jobId}/cancel`

#### Policies

- `GET /api/v1/policies/retention`
- `POST /api/v1/policies/retention`
- `GET /api/v1/policies/execution`
- `POST /api/v1/policies/execution`

#### Secrets y storage profiles

- `GET /api/v1/storage-profiles`
- `POST /api/v1/storage-profiles`
- `POST /api/v1/storage-profiles/{id}/validate`
- `GET /api/v1/secrets`
- `POST /api/v1/secrets`

#### Auditoría

- `GET /api/v1/audit/events`
- `GET /api/v1/audit/events/{eventId}`

## Protocolo Control Plane <-> Worker

### Recomendación

Comenzar con un modelo `pull` del worker para reducir complejidad de exposición de red:

- el worker abre sesión segura con el CP,
- reporta heartbeats,
- consulta comandos pendientes,
- ejecuta,
- reporta progreso y resultado.

Esto evita requerir conectividad entrante al worker.

### Canales

- `heartbeat`
- `inventory sync`
- `command fetch`
- `job status update`
- `log streaming`
- `certificate rotation`

### Comandos lógicos mínimos

- `inventory.refresh`
- `backup.run`
- `restore.dry_run`
- `restore.run`
- `snapshots.list`
- `stats.get`
- `retention.apply`
- `containers.stop`
- `containers.start`
- `containers.restart`
- `worker.self_check`

## Integración con Docker

### Restricción clave

El acceso a Docker debe permanecer local al host.

### Responsabilidades del Worker Agent

- inspeccionar contenedores,
- inspeccionar mounts y volúmenes,
- resolver qué contenedores usan un volumen,
- ejecutar stop/start/restart,
- lanzar contenedores efímeros del runtime de backup,
- mapear labels y compose projects.

### Modo frío

Para `cold backup`:

1. descubrir contenedores asociados al target,
2. validar política,
3. detener contenedores,
4. esperar ventana de estabilización opcional,
5. ejecutar backup,
6. reiniciar contenedores,
7. validar health opcional.

### Modo caliente

Para `hot backup`:

- no se detienen servicios,
- se ejecuta backup según capacidad del target,
- pueden mantenerse hooks pre/post para dumps lógicos o flushes.

## Ejecución del runtime actual

### Decisión

No romper la compatibilidad del runtime actual.

### Estrategia

El worker traduce la configuración central a variables de entorno compatibles con la imagen existente:

- `BACKUP_STRATEGY`
- `RESTIC_REPOSITORY`
- `RESTIC_PASSWORD`
- `RCLONE_REMOTE`
- `BACKUP_ARCHIVE`
- `RESTORE_*`
- variables cloud
- mounts necesarios

### Resultado

La plataforma nueva actúa como orquestador y el runtime actual sigue siendo el motor de backup/restore.

## Gestión de secretos

### Requisitos

- secretos cifrados en reposo,
- desencriptados solo cuando el job lo requiere,
- alcance mínimo por worker y target,
- rotación soportada,
- auditoría de uso.

### Tipos de secretos

- credenciales `restic`
- credenciales de nube
- `rclone.conf`
- SSH keys para SCP
- certificados mTLS
- basic auth local
- secretos de sesión interna

### Manejo de `rclone.conf`

No tratar `rclone.conf` como archivo global del sistema.

Debe gestionarse como secreto versionado por:

- storage profile,
- target,
- o tenant lógico.

El worker debe materializarlo en archivo temporal para el job y destruirlo al finalizar.

## Retención y housekeeping

### Política central

La retención se define en el Control Plane y se aplica desde el worker usando `restic forget --prune`.

### Parámetros mínimos

- `keep_last`
- `keep_hourly`
- `keep_daily`
- `keep_weekly`
- `keep_monthly`
- `keep_yearly`
- `prune_after_backup`
- `check_after_prune`

### Recomendación operativa

Separar conceptualmente:

- política de creación de backups,
- política de retención,
- política de verificación.

## Restore

### Reglas

- todo restore real debe empezar con dry-run,
- todo restore destructivo debe requerir confirmación explícita,
- el plan de restore debe mostrar:
  - snapshot seleccionado,
  - target path,
  - contenedores afectados,
  - modo frío/caliente,
  - ownership esperado,
  - layout seleccionado.

### Soporte requerido

- restore de snapshot Restic,
- restore desde artefactos soportados por runtime actual,
- `RESTORE_LAYOUT=auto`,
- `RESTORE_CHOWN`,
- `RESTORE_STOP_CONTAINERS`,
- validación posterior.

## UI del Control Plane

### Módulos principales

- Dashboard general.
- Fleet de workers.
- Inventario por host.
- Targets de backup.
- Jobs y logs.
- Snapshots y restores.
- Policies.
- Secrets y storage profiles.
- Auditoría.
- Configuración de autenticación.

### Vistas clave

#### Fleet

- worker status,
- versión,
- último heartbeat,
- cantidad de targets,
- errores recientes.

#### Targets

- compose project,
- volúmenes,
- modo frío/caliente,
- política aplicada,
- último backup,
- próximo backup,
- estado de salud.

#### Restore

- listado de snapshots,
- dry-run visible,
- diff lógico del plan,
- confirmación destructiva,
- seguimiento del job.

## Autenticación y autorización

### Modos de autenticación

- `basic`
- `oidc`
- `saml`

### Recomendación

Priorizar `OIDC` para Entra ID si es viable, ya que simplifica integración moderna.

### Roles sugeridos

- `admin`
- `operator`
- `auditor`
- `viewer`

### Reglas RBAC

- solo `admin` gestiona auth, certificados y secretos globales,
- `operator` puede ejecutar backups/restores,
- `auditor` ve metadatos y auditoría, pero no ejecuta,
- `viewer` solo consulta estado.

## Auditoría

### Eventos que deben auditarse

- login/logout,
- cambios de configuración,
- alta/baja de workers,
- rotación de certificados,
- creación/modificación de secretos,
- ejecuciones manuales,
- restores,
- operaciones de stop/start/restart,
- cambios de políticas,
- accesos a material sensible.

### Contenido mínimo del evento

- actor,
- acción,
- recurso,
- timestamp,
- dirección origen,
- correlación,
- resultado,
- motivo o payload resumido.

## Observabilidad

### Métricas mínimas

- workers conectados,
- heartbeats fallidos,
- jobs por estado,
- duración por tipo de job,
- éxito/fallo de backups,
- éxito/fallo de restores,
- snapshots por target,
- drift de inventario,
- vencimiento de certificados.

### Logs

- logs estructurados por job,
- correlación por `job_id`,
- trazabilidad entre CP y worker.

## Estructura de código propuesta

### Estrategia de repositorio

Para minimizar riesgo, mantener el runtime actual y agregar dos módulos nuevos.

```text
src/
  app/                        # runtime actual de backup/restore
  control_plane/
    api/
    application/
    domain/
    infrastructure/
    auth/
    scheduler/
    ui_backend/
  worker_agent/
    api_client/
    application/
    domain/
    infrastructure/
    docker_runtime/
    job_runner/
```

### Responsabilidad por módulo

- `src/app`: motor actual reutilizado.
- `src/control_plane`: orquestación, metadatos, auth, secretos, auditoría, scheduler.
- `src/worker_agent`: bridge local hacia Docker y jobs de ejecución.

## Fases de implementación

### Fase 1

- modelo de datos base,
- registro de workers,
- heartbeat,
- inventario básico Docker,
- definición de targets,
- backup on-demand,
- scheduler central simple,
- logs y jobs.

### Fase 2

- snapshots list y catálogo,
- retención centralizada,
- storage profiles,
- secretos cifrados,
- `rclone.conf` gestionado,
- restore dry-run,
- stop/start de contenedores.

### Fase 3

- restore real guiado,
- validaciones post-restore,
- OIDC/SAML,
- RBAC completo,
- rotación de certificados,
- vistas de auditoría.

### Fase 4

- verificación automática de backups,
- aprobaciones operativas,
- políticas avanzadas por grupos,
- delegación multi-tenant si llega a ser necesaria.

## Riesgos y mitigaciones

### Riesgo 1: demasiada lógica nueva en el worker

Mitigación:

- reutilizar el runtime existente,
- mantener al worker como orquestador local, no como motor de backup.

### Riesgo 2: dependencia excesiva de `docker.sock`

Mitigación:

- acceso solo local,
- privilegios mínimos,
- separación clara entre CP y Worker,
- auditoría estricta.

### Riesgo 3: manejo inseguro de secretos

Mitigación:

- vault interno cifrado,
- materialización temporal,
- rotación,
- no hardcodear secretos ni salts.

### Riesgo 4: restores destructivos mal operados

Mitigación:

- dry-run obligatorio,
- confirmación explícita,
- auditoría,
- parada controlada de servicios,
- validación posterior.

## Decisiones recomendadas

### Decisión 1

Adoptar `OIDC` como modo preferido para Entra ID, dejando `SAML` como opción secundaria y `basic` como fallback parametrizable.

### Decisión 2

Usar modelo `worker pull` en la primera versión para simplificar conectividad y seguridad.

### Decisión 3

Mantener el runtime actual del proyecto como motor único de backup/restore en lugar de crear un segundo motor.

### Decisión 4

Modelar `rclone.conf` como secreto versionado y no como archivo estático desplegado manualmente.

## Entregable de siguiente paso

Con esta especificación, el siguiente trabajo debe ser:

1. crear el modelo de dominio del `Control Plane`,
2. crear el esqueleto del `Worker Agent`,
3. definir los contratos API,
4. implementar enrolamiento seguro,
5. integrar la ejecución del runtime actual como job remoto.
