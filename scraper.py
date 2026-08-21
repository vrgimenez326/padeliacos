# -*- coding: utf-8 -*-
"""
Scraper de metapadel.com.ar
----------------------------
Lee las 3 categorías (Paletas Importadas, Paletas Nacionales, Bolsos),
extrae nombre / precio / imagen / stock de cada producto, aplica un
20% de descuento y guarda todo en catalogo.json.

Cómo correrlo (Windows):
  1) Instalar Python (python.org) si no lo tenés, marcando la opción
     "Add Python to PATH" durante la instalación.
  2) Abrir la carpeta en una consola (cmd o PowerShell) y ejecutar:
        pip install requests beautifulsoup4
  3) Ejecutar:
        python scraper.py
  4) Se genera un archivo catalogo.json con el resultado.

Si algo no se lee bien (nombre vacío, precio en 0, etc.), correr con:
        python scraper.py --debug
y mandarme la salida: eso me dice qué ajustar en los selectores.
"""

import json
import re
import sys
import time
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

DESCUENTO = 0.20  # 20% que se resta al precio de metapadel.com.ar

CATEGORIAS = {
    "importadas": "https://www.metapadel.com.ar/paletas-importadas/",
    "nacionales": "https://www.metapadel.com.ar/paletas-nacionales/",
    "bolsos": "https://www.metapadel.com.ar/bolsos/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DEBUG = "--debug" in sys.argv


@dataclass
class Producto:
    nombre: str
    categoria: str
    url: str
    imagen: str
    precio_origen: float
    precio_final: float
    en_stock: bool


def normalizar_imagen(url: str) -> str:
    """Convierte URLs relativas o protocol-relative en absolutas."""
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"https://www.metapadel.com.ar{url}"
    return url


def limpiar_precio(texto: str) -> float:
    """Convierte '$525.000,00' -> 525000.0"""
    if not texto:
        return 0.0
    texto = texto.replace("\xa0", " ").strip()
    numeros = re.sub(r"[^\d,\.]", "", texto)
    # formato AR: punto = miles, coma = decimales
    numeros = numeros.replace(".", "").replace(",", ".")
    try:
        return float(numeros)
    except ValueError:
        return 0.0


def extraer_via_jsonld(soup: BeautifulSoup, categoria: str):
    """
    Muchos sitios Tiendanube incluyen datos estructurados para Google
    (schema.org Product / ItemList) en <script type="application/ld+json">.
    Es la forma más confiable de leer nombre/precio/imagen si está presente.
    """
    productos = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (TypeError, ValueError):
            continue

        items = []
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            items = data.get("itemListElement", [])
        elif isinstance(data, list):
            items = data

        for item in items:
            item = item.get("item", item) if isinstance(item, dict) else {}
            if not item or item.get("@type") not in ("Product", None):
                continue
            oferta = item.get("offers", {})
            if isinstance(oferta, list):
                oferta = oferta[0] if oferta else {}

            precio = float(oferta.get("price", 0) or 0)
            disponibilidad = str(oferta.get("availability", "")).lower()
            en_stock = "outofstock" not in disponibilidad and "sin_stock" not in disponibilidad

            nombre = item.get("name")
            url = item.get("url") or oferta.get("url")
            imagen = item.get("image")
            if isinstance(imagen, list):
                imagen = imagen[0] if imagen else ""

            if nombre and precio:
                productos.append(Producto(
                    nombre=nombre,
                    categoria=categoria,
                    url=url or "",
                    imagen=normalizar_imagen(imagen),
                    precio_origen=precio,
                    precio_final=round(precio * (1 - DESCUENTO), 2),
                    en_stock=en_stock,
                ))
    return productos


def extraer_via_html(soup: BeautifulSoup, categoria: str):
    """
    Fallback: recorre los links a fichas de producto (/productos/...)
    y busca nombre, precio e imagen alrededor de cada uno.
    Es más frágil que el JSON-LD: si metapadel cambia el HTML, hay que
    ajustar esta parte.
    """
    productos = []
    vistos = set()

    for link in soup.select('a[href*="/productos/"]'):
        url = link.get("href")
        if not url or url in vistos:
            continue

        contenedor = link
        # subimos hasta encontrar un bloque que tenga precio adentro
        for _ in range(4):
            if contenedor.parent:
                contenedor = contenedor.parent
            texto_bloque = contenedor.get_text(" ", strip=True)
            if "$" in texto_bloque:
                break

        texto_bloque = contenedor.get_text(" ", strip=True)
        precios = re.findall(r"\$\s?[\d\.]+,\d{2}", texto_bloque)
        if not precios:
            continue

        precio_origen = limpiar_precio(precios[0])
        if precio_origen <= 0:
            continue

        nombre = (link.get("title") or link.get_text(" ", strip=True) or "").strip()
        nombre = re.sub(r"\s+", " ", nombre)
        # a veces el título del link repite la imagen alt varias veces
        if nombre:
            mitad = len(nombre) // 2
            if nombre[:mitad].strip() == nombre[mitad:].strip():
                nombre = nombre[:mitad].strip()

        img = contenedor.find("img")
        imagen = ""
        if img:
            imagen = img.get("src") or img.get("data-src") or ""

        en_stock = "sin stock" not in texto_bloque.lower()

        if nombre and precio_origen:
            vistos.add(url)
            productos.append(Producto(
                nombre=nombre,
                categoria=categoria,
                url=url if url.startswith("http") else f"https://www.metapadel.com.ar{url}",
                imagen=normalizar_imagen(imagen),
                precio_origen=precio_origen,
                precio_final=round(precio_origen * (1 - DESCUENTO), 2),
                en_stock=en_stock,
            ))

    return productos


def scrapear_categoria(nombre_categoria: str, url_base: str):
    productos = []
    pagina = 1

    while True:
        url = url_base if pagina == 1 else f"{url_base}?page={pagina}"
        if DEBUG:
            print(f"[{nombre_categoria}] pidiendo {url}")

        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        encontrados = extraer_via_jsonld(soup, nombre_categoria)
        metodo = "json-ld"
        if not encontrados:
            encontrados = extraer_via_html(soup, nombre_categoria)
            metodo = "html"

        if not encontrados:
            if DEBUG:
                print(f"[{nombre_categoria}] página {pagina}: sin productos, se corta acá")
            break

        nuevos = [p for p in encontrados if p.url not in {x.url for x in productos}]
        if not nuevos:
            break

        if DEBUG:
            print(f"[{nombre_categoria}] página {pagina}: {len(nuevos)} productos ({metodo})")

        productos.extend(nuevos)
        pagina += 1
        time.sleep(1)  # no golpear el sitio muy seguido

        if pagina > 40:  # tope de seguridad
            break

    return productos


def main():
    catalogo = []
    for nombre_categoria, url in CATEGORIAS.items():
        productos = scrapear_categoria(nombre_categoria, url)
        print(f"{nombre_categoria}: {len(productos)} productos")
        catalogo.extend(productos)

    with open("catalogo.json", "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in catalogo], f, ensure_ascii=False, indent=2)

    print(f"\nListo. {len(catalogo)} productos guardados en catalogo.json")


if __name__ == "__main__":
    main()
