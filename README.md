# Pizarra

Dibuja encima de toda la pantalla en Hyprland (Wayland). Usa Python, GTK3 y gtk-layer-shell.

- **SUPER + D**: activa o desactiva la pizarra (`pizarra.py --toggle`)
- **Herramientas**: lápiz, línea, flecha, círculo y rectángulo (barra superior o teclas 1–5 / P L A O R)
- **Mover**: arrastra una figura desde su borde (o muy cerca, el cursor cambia); por dentro, mantenla pulsada ~0,25 s sin mover el ratón y arrástrala (Shift: solo horizontal o vertical); después, las flechas la mueven 1 px (Shift: 10 px)
- **Color por defecto**: azul
- **Clic izquierdo**: dibujar · **Clic derecho**: borrar los trazos que tocas
- **Shift**: líneas a 15°, círculo y cuadrado perfectos
- **Rueda o + / -**: grosor · **Ctrl+Z / Ctrl+Y**: deshacer / rehacer · **Supr**: borrar todo
- **H**: ocultar la barra · **Esc**: salir

Dependencias (Arch): `python-gobject gtk3 gtk-layer-shell python-cairo`

El atajo está al final de `~/.config/hypr/hyprland.lua`, en el bloque `>>> pizarra`.
La copia de seguridad previa es `hyprland.lua.antes-pizarra`.
