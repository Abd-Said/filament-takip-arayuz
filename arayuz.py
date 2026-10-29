
import zipfile
import json
import os
import re
import xml.etree.ElementTree as ET


def bambu_filament_bilgisi(dosya_yolu):
    if not os.path.exists(dosya_yolu):
        print(f"Hata: {dosya_yolu} bulunamadı!")
        return

    with zipfile.ZipFile(dosya_yolu, "r") as z:
        isimler = z.namelist()
        slotlar = {}

        # Proje ayarlarından filament türü ve renklerini oku
        if "Metadata/project_settings.config" in isimler:
            ayar = json.loads(
                z.read("Metadata/project_settings.config")
            )

            turler = ayar.get("filament_type", [])
            renkler = ayar.get("filament_colour", [])

            for i in range(max(len(turler), len(renkler))):
                tur = turler[i] if i < len(turler) else "Bilinmiyor"
                renk = renkler[i] if i < len(renkler) else "Bilinmiyor"

                slotlar[i + 1] = {
                    "tur": tur,
                    "renk": renk,
                    "gram": 0.0
                }

        bulundu = False

        # 1) Dilimleme metadata bilgisi
        if "Metadata/slice_info.config" in isimler:
            kok = ET.fromstring(
                z.read("Metadata/slice_info.config")
            )

            for fil in kok.iter("filament"):
                bulundu = True

                # ID'nin slot numarası olup olmadığını kontrol et
                fid = fil.get("id")
                gram = float(fil.get("used_g", 0) or 0)
                renk = fil.get("color")
                tur = fil.get("type")

                try:
                    slot_no = int(fid)
                except (TypeError, ValueError):
                    slot_no = None

                if slot_no is not None:
                    slot = slotlar.setdefault(
                        slot_no,
                        {
                            "tur": tur or "Bilinmiyor",
                            "renk": renk or "Bilinmiyor",
                            "gram": 0.0
                        }
                    )
                    slot["gram"] += gram

                    if renk:
                        slot["renk"] = renk
                    if tur:
                        slot["tur"] = tur

                print(
                    f"Filament ID {fid}: {tur} | "
                    f"{renk} | {gram:.2f} g"
                )

        # 2) Metadata yoksa G-code başlığından oku
        if not bulundu:
            gcodelar = [
                n for n in isimler
                if n.lower().endswith(".gcode")
            ]

            for g in gcodelar:
                metin = z.read(g).decode(
                    "utf-8", errors="ignore"
                )

                m = re.search(
                    r"; filament used \[g\]\s*=\s*(.+)",
                    metin
                )

                if m:
                    bulundu = True
                    gramlar = [
                        float(x.strip())
                        for x in m.group(1).split(",")
                    ]

                    for i, gram in enumerate(gramlar, start=1):
                        slot = slotlar.setdefault(
                            i,
                            {
                                "tur": "Bilinmiyor",
                                "renk": "Bilinmiyor",
                                "gram": 0.0
                            }
                        )
                        slot["gram"] += gram

        # 3) Renk bazında raporla
        print("\n--- Filament tüketimi ---")

        if not bulundu:
            print(
                "Gramaj bulunamadı. Dosya dilimlenmiş "
                "G-code içermiyor olabilir."
            )
            return

        toplam = 0.0

        for no, bilgi in sorted(slotlar.items()):
            gram = bilgi["gram"]
            renk = bilgi["renk"]

            print(
                f"Slot {no}: Renk={renk} | "
                f"Tür={bilgi['tur']} | "
                f"{gram:.2f} g"
            )
            toplam += gram

        print(f"\nToplam tüketim: {toplam:.2f} g")


bambu_filament_bilgisi("dilim.gcode.3mf")
