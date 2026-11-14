# FireDeck 🔥

Webapp local para controlar un **Fire TV Stick** por **ADB inalámbrico** desde el navegador.
Sin cables, sin nube y sin dependencias de Python: solo la biblioteca estándar.

Incluye control remoto, captura en vivo con toque sobre la pantalla, grabación en MP4,
lista de apps con sus iconos reales, ficha del dispositivo, instalación de APK y paquetes
divididos (`.apks`, XAPK, APKM), descarga de APK, gestor de archivos, herramientas de red,
liberación de caché, una consola ADB y varios Fire TV a la vez desde pestañas distintas.

---

## Requisitos

- Windows, macOS o Linux con **Python 3.9+**.
- **Android Platform Tools** (`adb`) en el `PATH`, o déjalo en `platform-tools/platform-tools/adb.exe`.
  Si no lo tienes, arranca con `python app.py --download-adb` y FireDeck lo descarga de Google.
- El PC y el Fire TV en la **misma red Wi‑Fi**.

Comprobar que adb funciona:

```powershell
adb version
```

---

## Arrancar

Doble clic en **`iniciar.bat`** (Windows) o:

```powershell
cd firedeck
python app.py --download-adb
```

Abre <http://localhost:8080>.

> FireDeck escucha solo en `127.0.0.1` por seguridad. Si quieres abrirlo desde el móvil,
> usa `python app.py --host 0.0.0.0` y entra desde `http://IP-DEL-PC:8080`.
> Cualquiera en tu red podría entonces controlar el Fire TV: hazlo solo en redes de confianza.

---

## Preparar el Fire TV (una sola vez)

1. **Ajustes → Mi Fire TV → Acerca de**.
2. Pulsa **7 veces** sobre *Build* hasta que aparezca «Ya eres desarrollador».
3. Vuelve a **Ajustes → Mi Fire TV → Opciones de desarrollador**.
4. Activa **Depuración ADB**.
5. Apunta la IP en **Ajustes → Mi Fire TV → Red** (por ejemplo `192.168.1.50`).

### Fire OS 7 (Fire TV Stick Lite, 2ª gen, etc.)

Puerto **5555** y no hace falta código. En FireDeck pulsa **Conectar**, escribe la IP y listo.
Si el televisor muestra un aviso de autorización, acéptalo con el control.

### Fire OS 8 (4K Max 2ª gen, Cube 3ª gen…)

Usa emparejamiento:

1. En el Fire TV: **Opciones de desarrollador → Depuración inalámbrica → Emparejar dispositivo**.
2. Aparece una IP, un **puerto aleatorio** y un **código de 6 dígitos**.
3. En FireDeck marca **«Fire OS 8: requiere emparejamiento»**, escribe IP, puerto de
   emparejamiento y código → **Conectar**. El botón **Detectar** consulta mDNS y suele
   rellenar el puerto de emparejamiento solo.
4. Después FireDeck se conecta al puerto 5555 para enviar comandos.

> El puerto de emparejamiento cambia cada vez; el 5555 suele mantenerse.
> Si `adb connect` contesta `failed to authenticate`, casi siempre es que se está usando
> el puerto 5555 para emparejar en vez del puerto alto que muestra la TV.

### Dispositivos guardados

FireDeck recuerda los Fire TV a los que te conectas. En el diálogo **Conectar** aparecen
bajo **Dispositivos guardados** con su IP y su estado:

- Pulsa uno para reconectarte (o para elegirlo si ya está conectado).
- Pulsa **✎** para ponerle un nombre fácil de recordar, como «TV salón». Ese nombre se usa
  también en la cabecera de FireDeck.
- Pulsa **✕** para olvidarlo. La conexión actual no se corta.

La lista se guarda en `devices.json`, junto a `app.py`, así que sobrevive a reinicios del
servidor y no se envía a ninguna parte. FireDeck también recuerda cuál fue el último
dispositivo que elegiste y lo vuelve a seleccionar al arrancar si sigue conectado.
No se modifica nada en el Fire TV.

---

## Qué puedes hacer

| Pestaña | Función |
|---|---|
| **Control** | Espejo H.264 en vivo (WebCodecs) con toque y arrastre, capturas de respaldo cada 2 s, grabación de pantalla en MP4, D‑pad, OK, atrás, home, menú, multimedia, volumen, encendido y teclado de texto. Atajos: flechas, Enter y Backspace. |
| **Apps** | Lista apps de usuario (o todas) con icono real, nombre legible, categoría y búsqueda; abre, detiene, borra datos, desinstala, muestra la ficha de cada app y descarga su APK. |
| **Sistema** | Apps del sistema con nivel de riesgo y estado activo/desactivado; desactiva con `pm disable-user`, reactiva con `pm enable`, restaura lo desactivado y protege paquetes críticos. |
| **Archivos** | Explora `/sdcard`, entra en carpetas, sube archivos arrastrando o con el botón, descarga, renombra, crea carpetas y borra con confirmación. |
| **Dispositivo** | Ficha del Fire TV: sistema, red, pantalla, CPU, RAM, almacenamiento, temperatura y tiempo encendido. |
| **Red** | Estado de ADB con latencia PC → TV, datos de Wi-Fi (SSID, señal, banda, seguridad), IP/máscara/puerta de enlace/MAC, pings desde el Fire TV, reconexión rápida y búsqueda de equipos que hayan cambiado de IP. |
| **Instalar APK** | Descarga por código de Downloader, URL directa o enlace de MediaFire, o sube un `.apk`, `.apks`, `.xapk`, `.apkm` o `.zip`; instala APK únicos con `adb install` y paquetes divididos con `adb install-multiple`. |
| **Almacenamiento** | `df -h /data` y liberación de caché con `pm trim-caches`. |
| **Consola** | Cualquier comando `adb shell`, con atajos y reinicio del dispositivo. |

### Nombres de las apps

Android identifica cada app por su paquete (`com.netflix.ninja`), no por su nombre. El nombre
real está dentro del APK (`resources.arsc`) y leerlo exigiría descargar cada APK por Wi-Fi,
así que FireDeck usa dos vías:

1. Un catálogo de las apps habituales en Fire TV y Android TV (Netflix, Prime Video, Kodi,
   SmartTube, Plex, Disney+…) que muestra el nombre y la categoría oficiales.
2. Para el resto, reconstruye el nombre desde el identificador: `com.tubitv` → «Tubitv»,
   `org.foo.downloader` → «Foo Downloader». Si el nombre se dedujo, la ficha lo indica.

La pestaña **Apps** busca por nombre, paquete o categoría, y el botón **Info** de cada fila
consulta al Fire TV la versión, el tamaño, las fechas de instalación/actualización, la
actividad de lanzamiento y la ruta del APK. Pulsar el identificador lo copia al portapapeles.
Si un nombre deducido no te dice nada, desde **Info → Poner un nombre a esta app** puedes
renombrarla; el alias se guarda en el navegador (no se toca el Fire TV).

### Iconos reales

Al abrir **Apps**, FireDeck pide a la TV la ruta del APK de cada app (`pm path`) y lee el
`android:icon` real de su manifiesto. No baja el APK entero: pide por Wi‑Fi solo los trozos que
necesita (el final del ZIP, el manifiesto, `resources.arsc` y el propio icono, con `dd`), lo que
suele ser de cientos de KB en vez de decenas de MB. Si eso falla (poco habitual), vuelve a la
descarga completa, que solo se hace con APK de hasta 250 MB. Si el icono es PNG, WebP o JPEG se
sirve tal cual; si es un icono adaptativo o vectorial, FireDeck reconstruye un SVG con sus
capas (fondo y símbolo) para que el navegador lo dibuje nítido. No usa herramientas externas
ni envía nada a internet. El resultado se guarda en `.cache/icons`, así que la primera carga
puede tardar unos segundos por app y las siguientes son inmediatas.

Los APK con nombres de recursos ofuscados también funcionan: la ruta se resuelve leyendo
`resources.arsc` y el manifiesto binario, no adivinando por el nombre del archivo. Si una app
no tiene ningún icono utilizable —por ejemplo, las capas de sistema de Fire OS—, se muestra la
inicial del nombre con un color propio. La extracción se reintenta sola pasados unos minutos.

### Ficha del dispositivo

La pestaña **Dispositivo** lee del propio Fire TV, sin modificar nada: modelo, marca, Fire OS,
Android/API, número de serie, IP, MAC, resolución, densidad, procesador, núcleos, uso de CPU en
ese instante, RAM, almacenamiento, tiempo encendido y sensores térmicos. La temperatura y el uso
de CPU son una foto del momento; algunos modelos (como el Fire TV Stick Lite) no publican
sensores térmicos y la tarjeta lo indica.

### Tocar y deslizar sobre la captura

Con una captura en pantalla puedes **hacer clic** para tocar ese punto del Fire TV (`input tap`)
o **arrastrar** para deslizar (`input swipe`). FireDeck convierte la posición del ratón a
coordenadas del televisor y dibuja un pequeño círculo donde has pulsado. Funciona con la pantalla
encendida; si la app usa DRM, la captura sale en negro y no hay nada que pulsar.

### Espejo en vivo

La casilla **En vivo** de la tarjeta Pantalla abre un flujo H.264 con `screenrecord` del Fire TV
y lo decodifica en el propio navegador con **WebCodecs** (Chrome/Edge 94 o posterior), sin ffmpeg,
VLC ni plugins. La imagen se pinta en un canvas y el toque/arrastre funciona igual que sobre una
captura.

Fire OS no repinta el vídeo cuando nada cambia: `screenrecord` solo entrega fotogramas cuando la
pantalla se actualiza. En la práctica el espejo se queda quieto en un menú estático (sin gastar
ancho de banda) y se refresca al navegar; medido en un Fire TV Stick, unos 5 fps con movimiento. El
primer fotograma tarda unos 5–8 s porque antes hay que comprobar el estado del televisor y cerrar
cualquier `screenrecord` anterior; si no llega, FireDeck sigue con capturas. El flujo se reinicia solo
cada 180 s, que es el máximo de `screenrecord`.

Si el navegador no tiene WebCodecs, el televisor está en suspensión o el flujo se corta, FireDeck
vuelve automáticamente a capturas cada 2 s y lo indica en la etiqueta de la esquina. Al desactivar
**En vivo** se detiene `screenrecord` en el televisor. El contenido con DRM sigue saliendo en negro.

### Grabar la pantalla

El botón **Grabar** de la tarjeta Pantalla usa `screenrecord` del propio Fire TV para grabar
15, 30 o 60 segundos en MP4 (el servidor admite hasta 180 s por API). Puedes detenerlo antes y
descargar el vídeo cuando termine; FireDeck lo copia al PC y borra el archivo temporal del
televisor. Si la pantalla está en suspensión la grabación saldría en negro y FireDeck lo avisa
antes de empezar. El audio del televisor y el vídeo con DRM no se pueden capturar por ADB.

### Varias pestañas, varios Fire TV

Cada pestaña del navegador es un mando independiente. Puedes tener FireDeck abierto dos veces y
controlar **Sala** en una pestaña y **Cuadro** en la otra sin que se pisen:

- La cabecera indica **«Esta pestaña controla Sala»** y, debajo, **«Otras pestañas: Pestaña 2 →
  Cuadro»**, para saber en todo momento qué manda cada una.
- Una pestaña nueva hereda el último Fire TV que usaste. En cuanto eliges otro en **Gestionar**,
  la elección queda fijada solo para esa pestaña.
- En **Dispositivos guardados** cada equipo muestra en qué otras pestañas está en uso.
- Capturas, espejo, grabación, apps, ficha del dispositivo, archivos, sistema, red y consola se
  resuelven siempre con el Fire TV de la pestaña que hace la petición. El servidor lo distingue
  por la cabecera `X-FireDeck-Tab`; no hay que configurar nada.
- Si duplicas una pestaña desde el menú del navegador, FireDeck detecta el identificador repetido
  y le asigna uno propio para que no comparta el control con la original.

Límites: el espejo y la grabación pertenecen al televisor, no a la pestaña. Si dos pestañas
apuntan al mismo Fire TV y ambas activan **En vivo**, la segunda toma el control del flujo y la
primera deja de recibir vídeo (vuelve a capturas). Una pestaña cuenta como abierta mientras hace
sus consultas periódicas; al cerrarla desaparece de la lista de las demás en un par de minutos.
El dispositivo elegido se recuerda seis horas aunque la pestaña esté inactiva.

### Descargar el APK de una app

En **Apps → Info** aparece **Descargar APK**: copia el APK instalado en el Fire TV a este PC.
Si la app usa APK divididos, descarga un ZIP con el APK base y sus divisiones. Es útil para
conservar una versión que ya no está disponible o para reinstalarla en otro equipo. Respeta la
licencia de cada app: es una copia para uso personal, no una forma de saltarse DRM.

### Instalar APK y paquetes divididos

En **Instalar APK** puedes soltar un `.apk` suelto o un paquete `.apks`, `.xapk`, `.apkm`
o `.zip` con varios APK. No hace falta descomprimirlo:

- **APK único:** se envía y se instala con `adb install -r -g`.
- **Paquete dividido:** FireDeck abre el ZIP, localiza el APK base y sus divisiones, descarta
  las de arquitecturas que el Fire TV no admite y lo instala todo junto con
  `adb install-multiple -r -g`. El log final indica cuántos APK se instalaron, el tamaño, la
  firma y cuántos splits se omitieron.
- Antes de enviar nada comprueba que todos los APK declaran el mismo paquete, que comparten el
  mismo certificado de firma y que la base no exige una versión de Android superior. Fire OS
  sigue siendo quien valida criptográficamente la firma al instalar.
- El archivo se guarda en un temporal del PC y se borra al terminar. El límite es 3 GB por
  subida, 4 GB ya descomprimido y 200 APK por paquete.
- Si el paquete contiene **varias variantes base** (por ejemplo un `.apks` con una base por
  arquitectura), FireDeck lo rechaza y explica cómo elegir una: usa el XAPK o el APK universal
  de una sola variante.
- Los **OBB** que a veces incluye un XAPK no se instalan. Si la app los necesita, copia la
  carpeta `Android/obb/<paquete>/` a la misma ruta del Fire TV desde **Archivos**.
- Verificado en un Fire TV con Android 9 (API 28) con un paquete de prueba firmado con la clave
  de depuración: un `.apks` con base + `config.armeabi_v7a` instaló los dos APK (`pm path`
  devolvió ambas rutas), un `config.x86_64` incluido a propósito se omitió por arquitectura,
  y los paquetes con firmas distintas o con dos bases se rechazaron antes de enviarlos.

### Descargar por código o URL

La app **Downloader** de AFTVnews no descarga «por código»: cambia ese número por una URL en
`aftv.news` y baja el archivo. **Instalar APK** hace lo mismo desde el PC, sin instalar
Downloader en el televisor:

1. Escribe el código (3–32 letras o números), pega un enlace `http://`/`https://` directo o
   la página de un archivo de MediaFire.
2. **Resolver** muestra el dominio real, el nombre del archivo, su tamaño y el tipo, sin bajar
   nada todavía.
3. **Descargar e instalar** baja el archivo con progreso y barra, y al terminar reutiliza la
   instalación de la sección anterior: APK único o paquete dividido con sus splits y filtrado
   por arquitectura. Puedes **Cancelar** mientras descarga (no durante la instalación).
4. Los últimos códigos y enlaces usados quedan como botones en **Recientes**, en
   `downloader.json`. No se envía nada a ningún servicio de FireDeck: el único tercero es el
   redirector de AFTVnews cuando usas un código.

El redirector de AFTVnews no responde con un 302: devuelve una página con una espera de 6
segundos y `window.location`. FireDeck lee el destino de esa página y lo sigue al instante, sin
esperar ni ejecutar JavaScript.

**MediaFire** se acepta tal cual, sin buscar el enlace directo: la página del archivo no es el
archivo, así que FireDeck lee su botón **Download**, extrae la dirección real del CDN
(`download####.mediafire.com`) y la descarga. Sirve para `mediafire.com/file/…` y para los enlaces
antiguos `mediafire.com/?…`. Si el archivo es privado, fue borrado o el enlace es de una carpeta,
lo explica en vez de descargar una página HTML.

Límites y seguridad: 3 GB por descarga, 6 redirecciones y solo `http`/`https`. FireDeck resuelve
el DNS y rechaza `localhost`, rangos privados (`10.x`, `192.168.x`, `172.16–31.x`), enlaces con
usuario/contraseña y certificados TLS inválidos, también en cada redirección, para que un código
malicioso no pueda leer tu red local. El enlace extraído de MediaFire también debe pertenecer a
MediaFire y pasa por las mismas comprobaciones. Si otra página web (Dropbox, Google Drive…) no es
un archivo directo, FireDeck lo dice y no intenta adivinar el botón de descarga: abre la página,
copia el enlace directo y pégalo en el mismo campo. Un código caducado también se rechaza con un
mensaje claro.

### Gestor de archivos

La pestaña **Archivos** muestra la carpeta compartida del Fire TV (`/sdcard`), con el nombre,
el tamaño y la fecha de cada elemento. Pulsa una carpeta para entrar; la barra superior y el
botón **↑** sirven para moverte por la ruta, y **Actualizar** vuelve a leerla.

- **Subir:** pulsa **Subir archivos** o arrastra archivos desde el PC hasta la lista. Si ya
  existe uno con el mismo nombre, FireDeck pide confirmación antes de reemplazarlo. Los
  archivos viajan por la red local y se copian con `adb push`; al terminar se comprueba que
  el tamaño en el Fire TV coincide con el original.
- **Descargar:** pulsa el nombre del archivo o **Descargar**. Se copia al PC con `adb pull`.
- **Renombrar / Nueva carpeta:** usan `mv` y `mkdir`; los nombres no pueden contener barras.
- **Borrar:** pide confirmación y usa `rm -rf`. Si borras una carpeta, se borra también todo
  su contenido. FireDeck nunca permite borrar `/sdcard` ni salir de esa carpeta con `..`.

Solo se gestiona `/sdcard` (la carpeta visible para el usuario). Algunas rutas del sistema
están protegidas por Android y fallarán al escribir o borrar sin root; FireDeck muestra el
error tal cual. Para ver el espacio libre, usa la pestaña **Almacenamiento**.

### Desactivar apps del sistema (debloat)

La pestaña **Sistema** lista los paquetes del sistema (`pm list packages -s`) con su estado real:
activa o desactivada. `pm disable-user` oculta la app del Fire TV sin borrar su APK ni sus datos;
`pm enable` la devuelve a su sitio. No hace falta root.

- **Nivel de riesgo:** cada app aparece como **Segura**, **Precaución** o **Crítica**. El filtro
  **Solo seguras** (activo por defecto) muestra las apps opcionales —Amazon Music, Silk, compras,
  juegos…— y también las que ya hayas desactivado, para que puedas revertirlas.
- **Paquetes críticos:** FireDeck bloquea el inicio (`launcher`), la interfaz
  (`systemui`), los ajustes, el instalador de APK, el intérprete de ADB, el Bluetooth del mando,
  la red, el motor web y los servicios de cuenta de Amazon. El botón aparece como **Protegida**
  y el servidor rechaza la orden aunque se envíe por API.
- **Registro por dispositivo:** lo que desactivas se guarda en `debloat.json` junto a `app.py`,
  asociado a la IP/puerto del Fire TV. **Restaurar desactivadas** reactiva solo esas apps; no
  toca las que ya estaban desactivadas antes (por ejemplo, NFC). Si el Fire TV cambia de IP y
  **Buscar de nuevo** lo reconoce por su número de serie, el registro se traslada a la IP nueva.
- **Sin borrar nada:** el APK sigue en el Fire TV y puedes volver a activar la app en cualquier
  momento desde la misma lista. Si una app del sistema se actualiza, Android puede volver a
  activarla: basta con desactivarla otra vez.

Desactivar una app de **Precaución** puede quitar funciones concretas (la tienda de apps, las
actualizaciones, el casting o Alexa). Hazlo solo si sabes para qué sirve; ante la duda, deja
**Solo seguras** marcado.

### Herramientas de red

La pestaña **Red** reúne el estado de la conexión y del Wi-Fi del Fire TV seleccionado:

- **Conexión ADB:** estado, IP y puerto, latencia PC → TV (media de 4 saludos TCP al puerto
  ADB, con mínimo, máximo y pérdida) y número de serie del equipo.
- **Wi-Fi:** SSID, intensidad (`dBm` y calidad), banda y frecuencia, velocidad de enlace,
  seguridad (WPA2-PSK, etc.) y si la IP llega por DHCP.
- **Dirección IP:** IP, máscara/prefijo, puerta de enlace, MAC del adaptador Wi-Fi e interfaz.
- **Desde el Fire TV:** ping al router y a internet (8.8.8.8) lanzados en el propio televisor,
  con pérdida de paquetes, y si Android da la red por validada.
- **Reconectar ADB:** corta y rehace `adb disconnect` / `adb connect` sin reiniciar el
  servidor. Útil cuando la captura o los comandos dejan de responder.
- **Buscar de nuevo:** recorre la red local (el `/24` del PC), conecta a los puertos 5555 y
  lee el número de serie de cada equipo. Si un Fire TV guardado aparece en otra IP, actualiza
  la lista conservando su nombre y muestra el cambio. Así se recupera un equipo tras un cambio
  de DHCP sin tener que buscarlo a mano.

No se puede encender un Fire TV apagado: Fire OS no admite Wake-on-LAN de forma fiable.
La lectura usa `dumpsys wifi`, `dumpsys connectivity`, `ip addr` y `ping`; no requiere root
ni envía datos fuera de la red local.

### Liberar caché

`pm trim-caches 512G` pide a Android que recorte caché de apps hasta tener ese espacio libre.
No borra cuentas ni datos. **Fire OS 7 no permite borrar la caché de una app concreta sin root**;
para eso existe «Borrar datos» (más agresivo: cierra sesión y borra configuración).

---

## Solución de problemas

**`failed to connect to ...:5555`**
- Verifica que la depuración ADB siga activa (se desactiva sola tras algunos reinicios).
- Comprueba la IP actual del Fire TV; suele cambiar con el DHCP.
- Prueba `ping IP-DEL-FIRE-TV` y `adb connect IP:5555` en una terminal.
- Redes de invitados o con aislamiento de clientes bloquean la conexión.

**`failed to authenticate to 192.168.x.x:5555`**
Significa que el Fire TV responde, pero no confía en la clave RSA de este PC (el aviso de
permiso no se aceptó, se revocó, o quedó una clave vieja). FireDeck reinicia solo el
servidor ADB una vez y reintenta; si sigue fallando, abre el panel y pulsa **Diagnóstico**:
comprueba la IP, el puerto, la clave local y el estado real de `adb devices`, y detecta por
mDNS el puerto de emparejamiento si es Fire OS 8.

Después:

1. En el televisor, **Ajustes → Mi Fire TV → Opciones de desarrollador → Revocar
   autorizaciones de depuración USB** (si existe en tu versión).
2. Pulsa **Reiniciar ADB** en FireDeck y vuelve a conectar.
3. Acepta **«¿Permitir depuración USB?»** en el televisor, marcando **Permitir siempre**.

En **Fire OS 8** este error también aparece si intentas conectar al 5555 sin emparejar
antes: activa **Emparejar dispositivo**, usa el puerto que muestra la TV (no 5555) y el
código de 6 dígitos.

Si no aparece el aviso, pulsa **Generar clave nueva**: aparta `%USERPROFILE%\.android\adbkey*`
(no las borra) y adb crea otra, con lo que el Fire TV vuelve a preguntar. Ojo: los demás
dispositivos Android tendrán que autorizar este PC otra vez.

También ayuda apagar y encender la depuración ADB, o reiniciar el Fire TV; con el televisor
apagado o en reposo el diálogo no se muestra y la autenticación falla.

**`unauthorized`**
- Acepta el diálogo «Permitir depuración USB» en el televisor con el control.
- Si no aparece: en el Fire TV desactiva y reactiva la depuración ADB, o usa
  **Reiniciar ADB** / `adb kill-server` y vuelve a conectar.

**Se queda en «Conectando…» o el dispositivo desaparece**
```powershell
adb kill-server
adb start-server
adb devices
```

**La captura sale negra**
Fire OS no deja capturar la capa de vídeo ni el contenido protegido por DRM (Netflix,
Prime Video, Disney+…). FireDeck analiza el fotograma crudo de `screencap` y, si es
prácticamente todo negro, muestra un aviso en la tarjeta de pantalla en lugar de un
rectángulo negro: indica si el Fire TV está en reposo o si la app usa una superficie
segura. Las capturas del menú y de apps sin DRM funcionan con normalidad. Comprueba
también que el televisor esté despierto (envía **Inicio** y vuelve a capturar).

**El APK no instala**

- Debe ser compatible con Android TV / Fire OS.
- `INSTALL_FAILED_NO_MATCHING_ABIS`: el APK no incluye la arquitectura del Fire TV. El
  **Fire TV Stick Lite (AFTSS)** y el Stick de 2ª gen son de **32 bits**
  (`armeabi-v7a`), así que un APK que solo traiga `arm64-v8a` o `x86` nunca instalará.
  FireDeck abre el APK antes de subirlo, compara `lib/<abi>/*.so` con
  `getprop ro.product.cpu.abilist` del televisor y, si no hay intersección, muestra qué
  ABIs trae el APK y cuáles admite el TV (sin gastar la subida por Wi-Fi).

  Solución: descarga la variante **universal** o **armeabi-v7a** (APKMirror tiene filtro
  de arquitectura; APKPure la marca como «universal») o una versión anterior de la app.
  Si solo publica arm64, esa app ya no soporta tu modelo.

  Comprobación manual de tu televisor:

  ```powershell
  adb shell getprop ro.product.cpu.abilist    # p. ej. armeabi-v7a,armeabi
  ```

- `INSTALL_FAILED_UPDATE_INCOMPATIBLE`: desinstala primero la versión existente (pestaña
  **Apps**).
- `INSTALL_FAILED_INSUFFICIENT_STORAGE`: libera caché o desinstala apps.
- `INSTALL_FAILED_VERSION_DOWNGRADE`: la versión instalada es más reciente; desinstálala
  antes de instalar esta.
- `INSTALL_PARSE_FAILED_*` / `INSTALL_FAILED_INVALID_APK`: el archivo está dañado o
  incompleto; vuelve a descargarlo. Si aparece `code is missing`, el APK base no contiene
  código y el paquete está mal construido.
- **«El paquete contiene varias variantes base»**: el `.apks` incluye una base por
  arquitectura o por SDK. Usa el XAPK o el APK universal de una sola variante.
- **«Los APK del paquete no tienen la misma firma»** o **«mezcla APK de apps distintas»**:
  el paquete está corrupto o se han juntado archivos de dos fuentes. Descárgalo de nuevo
  completo.
- **«No pude comprobar la firma»**: algún split está dañado o sin firmar. Vuelve a descargar
  el paquete; FireDeck no envía nada al Fire TV en ese caso.
- Si una app XAPK instala pero se cierra al abrir, probablemente necesita sus **OBB**. Esta
  versión instala solo los APK; copia los OBB a `/sdcard/Android/obb/<paquete>/` desde
  **Archivos**.

**No puedo subir, renombrar o borrar un archivo**

- FireDeck solo gestiona `/sdcard`. Las carpetas del sistema (`/data`, `/system`, ajustes de
  Android) no se tocan; usa el explorador para el almacenamiento compartido.
- Algunas rutas como `Android/data` están protegidas por Android 9 y fallarán sin root;
  FireDeck muestra el mensaje del sistema. Prueba en `Download` o crea una carpeta nueva.
- Si el nombre ya existe, la subida pide confirmación antes de reemplazar. Renombrar a un
  nombre ocupado se rechaza para no perder el otro archivo.
- Si una subida grande se corta, comprueba que el Fire TV siga despierto y en la misma red;
  el archivo se copia con `adb push` y se verifica su tamaño al terminar.

**Puerto 8080 ocupado**
```powershell
python app.py --port 8090
```

---

## Estructura

```
firedeck/
├── app.py              servidor HTTP + wrapper de ADB (stdlib)
├── devices.json        dispositivos guardados y sus nombres (se crea solo)
├── debloat.json        apps del sistema desactivadas desde FireDeck (se crea solo)
├── downloader.json     últimos códigos y URL usados para instalar (se crea solo)
├── .cache/icons/       iconos extraídos de los APK (se crea solo)
├── iniciar.bat         lanzador de Windows
├── .gitignore          excluye datos locales, caché, logs y platform-tools
├── README.md
├── tests/
│   ├── apkbuild.py     genera APK, manifiestos binarios y firmas sintéticos
│   └── test_parsers.py pruebas de los parseos (92 casos, solo stdlib)
├── tools/
│   └── asar-extract.js script suelto para extraer archivos .asar (Node)
└── static/
    ├── index.html
    ├── styles.css
    ├── app.js
    └── mirror.js       decodificador H.264 del espejo en vivo (WebCodecs)
```

Si compartes o subes el proyecto a un repositorio, los archivos de datos (`devices.json`,
`debloat.json`, `downloader.json`) y `.cache/` quedan fuera gracias al `.gitignore`: contienen
las IP de tu red doméstica y el historial de descargas.

---

## Pruebas

```powershell
python -m unittest discover -s tests -v
```

No necesitan Fire TV ni adb: construyen APK, manifiestos binarios (AXML), `resources.arsc`,
firmas v1/v2 y paquetes `.apks`/`.xapk` sintéticos y comprueban los parseos, el filtrado por
arquitectura, la validación de rutas de `/sdcard`, el rechazo de direcciones privadas en las
descargas y el control de origen de las peticiones.

---

## Seguridad

ADB inalámbrico da control total del dispositivo a quien pueda conectarse. Este proyecto
está pensado para una red doméstica de confianza. No expongas el puerto 8080 a internet,
no lo ejecutes en redes públicas y desactiva la depuración ADB del Fire TV cuando no la uses.

El servidor solo atiende peticiones de su propia interfaz: rechaza (403) las que llegan con un
`Host` que no sea `localhost` o una IP, con un `Origin` de otra web o con `Sec-Fetch-Site`
entre sitios, y las órdenes con cuerpo exigen `application/json`. Así, una página cualquiera
abierta en tu navegador no puede manejar el Fire TV a través de `localhost:8080` (CSRF) ni
mediante DNS rebinding. Si publicas FireDeck detrás de un nombre de dominio o un proxy inverso,
accede por IP o añade esa excepción en `request_allowed()`.

La descarga por código o URL solo acepta `http`/`https`, valida el DNS en cada redirección y
rechaza direcciones privadas, locales y reservadas. Así, un enlace malicioso no puede usar el
servidor de FireDeck para leer la red doméstica ni servicios que escuchen en `localhost`.

## Licencia

MIT. Consulta el archivo [LICENSE](LICENSE).

## Aviso

FireDeck es un proyecto independiente y no tiene relación con Amazon. Fire TV, Fire OS y Amazon son marcas de sus respectivos dueños. Úsalo solo con dispositivos tuyos o con permiso de su dueño.