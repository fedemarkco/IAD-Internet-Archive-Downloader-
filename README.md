# IAD (Internet Archive Downloader)
Descarga archivos desde https://archive.org/, con velocidades que pueden superar los 30 MB/s.

## Origen
He visto por internet que hay aplicaciones de terceros para descargar de Internet Archive, también extensiones para Chrome, en la página oficial de archive.org se encuentra otra aplicación y con internet download manager también es posible descargar de esta plataforma, pero el problema que tienen todos, o por lo menos yo lo veo de esa forma, es que descargan entre 2 a 5 MB/s. Cuando me ha tocado descargar de Internet Archive, varios archivos con un peso considerable (varios gigas), la descarga que obtenía era casi siempre de 2 MB/s, suponte que quieres descargar un archivo de 60 GB, con 2 MB/s de bajada vas a demorar aprox 10 horas, lo cual es muchísimo tiempo. Investigando por internet por esta lentitud en la descarga, sólo he encontrado quejas de varios usuarios en distintos foros o redes sociales, por ejemplo Reddit, y consultando a los dueños de la página, me informaban que estaban al tanto de la baja velocidad de descarga pero "que pensaban solucionarlo en algún futuro", o sea que su software oficial descarga lento y lo saben. Ante esto, me he puesto en marcha para encontrar una solución y así poder descargar rápido de esta platforma y es como surgió IAD (Internet Archive Downloader), este proyecto que he hecho permite lograr descargar archivos de Intenert Archive logrando velocidades superiores a los 30 MB/s. Le he creado una interfaz gráfica para que sea más amigable con el usuario final.

## Instalación
```
pip install PyQt5
pip install urllib3
pip install requests
```

## Funcionamiento
Al compilar el archivo python, te encontrarás con una ventana que te pide el usuario y contraseña de logueo de archive.org, cuando lo ingreses te llevará a otra ventana que te pedirá que ingreses una URL, seleccionar la carpeta donde quieres almacenar los archivos que se descarguen, y elegir la cantidad de archivos en paralelo a descargar. Ahora es cuando te preguntas, ¿Cuál URL tengo que ingresar? La respuesta es sencilla, la que tienes que descargar son las que comienzan con

**https://archive.org/download/**

Estos enlaces los encuentras si accedes a los ítems de lo que quieres descargar, para ser más explícito, si realizas la búsqueda de, por ejemplo, **roms snes**, el buscador te mostrará varios resultados, si eliges alguno de ellos, te llevará a una pantalla donde en el lado derecho dice **DOWNLOAD OPTIONS**, mostrando distintas formas de descarga del archivo, lo que tienes que hacer en este caso, es hacer clic a la opción **SHOW ALL**. Y la URL a la que te redireccione, que comenzará como te mencioné anteriormente, tienes que copiarla completa y en la aplicación pegarla en donde dice **URL**, luego hacer click en el botón **Search**. Te aparecerá una pequeña ventana para seleccionar/deseleccionar los archivos que quieres descargar, que se encuentren en la URL que ingresaste. Aceptas la ventana y comenzará la búsqueda de archivos que puedan contener tu selección, para ser mostrados en la grilla que aparece abajo. También deberás seleccionar la carpeta donde almacenar los archivos que descargues haciendo clic en el botón **Save To Folder** y como plus también te da la posibilidad de descargar hasta 32 archivos en paralelo eligiendo de la lista de **Thread**. Paso final, hacer click en el botón **Start**.

## Autor
**Marco Weihmüller**

## Licencia
Este proyecto está bajo la Licencia GNU General Public License v3.0
