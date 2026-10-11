# Yakala: tek tuşla beyne at

Video, tweet, makale, mail, PDF ya da aklına gelen bir cümle: gördüğün yerde tek tuşla yakala,
ajanın sonra okuyup dersleri ikinci beynine bağlasın. İsteğe bağlıdır; kurmazsan hiçbir şey değişmez.

## Kurulum (bir kez)

Vault klasöründe:

```sh
python3 beyin.py yakala kur
```

Windows'ta `py -3 beyin.py yakala kur`. Ya da ajanına "yakala aracını kur" de.

Kurulum şunları yapar:

| | Mac | Windows |
|---|---|---|
| Her uygulamada kısayol | `Control+Option+B` | `Ctrl+Alt+B` |
| Dosya gönderme | Finder'da dosyayı seç, kısayola bas | Sağ tık > Gönder > Beyne At |
| Tarayıcı | Obsidian Web Clipper şablonu | Obsidian Web Clipper şablonu |
| Ajan | `beyin-yakala` skill'i | `beyin-yakala` skill'i |

Kısayol ek paket istemez; Python'un kendisiyle çalışır. Mac'te bir oturum açılış servisi
(LaunchAgent) tuşu dinler, Windows'ta Başlat menüsündeki kısayol tuşu kullanılır.
Yalnız şablon ve skill istiyorsan: `python3 beyin.py yakala kur --kisayol-yok`.

### Kısayolu değiştir

```sh
python3 beyin.py yakala kisayol 'cmd+"'
```

Tuşu yazdığın gibi tarif et: `cmd`, `ctrl`, `alt` (ya da `option`), `shift` ve en sonda tuş.
Örnekler: `ctrl+alt+b` (varsayılan), `cmd+shift+space`, `alt+f5`, `⌘⇧K`. Mac'te karakter etkin klavye
düzeninden bulunur; Türkçe Q'da `"` 1'in solundaki tuştur. Windows'ta Başlat menüsü kısayolu
yalnız Ctrl/Alt/Shift ile harf, rakam ya da F tuşunu kabul eder. Argümansız `kisayol` mevcut tuşu
gösterir. Seçtiğin tuşu başka bir uygulama zaten kullanıyorsa komut uyarır; başka bir tuş dene.

Mac'te dinleyici arka planda çalışır, Dock'ta görünmez; kapanırsa sistem onu yeniden başlatır.
Dinleyici `kur`'un yazdığı bir kopyadan çalışır: bir güncellemeden sonra onu da yenilemek için
`python3 beyin.py yakala kur` komutunu yeniden çalıştır (tuşun korunur). Çalışıp çalışmadığını
`python3 beyin.py yakala durum` söyler.

### Tarayıcı: Obsidian Web Clipper

1. [Obsidian Web Clipper](https://obsidian.md/clipper) eklentisini kur (Chrome, Firefox, Safari, Edge, Arc, Brave).
2. Eklentinin ayarlarında **Şablonlar > İçe aktar** ile kurulumun kart klasörüne yazdığı
   `beyne-at-web-clipper.json` dosyasını seç (varsayılan `📥 000-Inbox/Yakala/`; tam yolu `kur` söyler).
3. Bir sayfada eklentiye bas, "Beyne at" şablonunu seç, istersen **Neden** bölümüne bir satır yaz, ekle.

Web Clipper sayfayı tarayıcının içinden okur; giriş isteyen sayfalar (Gmail, ücretli makale) da böylece yakalanır.

## Kullanım

1. **Yakala.** Kısayola bas. Pencere neyin kaydedileceğini gösterir: tarayıcıdaki sayfa
   (Mac'te Chrome, Arc, Brave, Edge, Safari), Finder'da seçili dosya ya da panodaki metin.
   İstersen tek satır "neden" yaz, Enter. Esc vazgeçer.
2. **İşlet.** Ajanına "yakalananları işle" de. Oturum başında bekleyen kaynak sayısı da görünür.

Her yakalama kart klasöründe (varsayılan `📥 000-Inbox/Yakala/`) bir kart olur. Aynı videoyu ya da
sayfayı ikinci kez yakalarsan yeni kart açılmaz; yeni notun aynı karta eklenir.

### Kartların klasörü

Gelen kutusu klasörlerinden yalnız birinde `Yakala/` zaten varsa kartlar oraya yazılır; böyle tek
bir klasör yoksa başlangıç klasörü `📥 000-Inbox` kullanılır, o da yoksa vault'un en üstünde adı gelen
kutusu olan tek klasör (`000-Inbox`, `00_INBOX`, `Gelen Kutusu`). Karar verilemiyorsa tahmin edilmez
ve başlangıç yolu açılır. Nokta ile başlayan klasörler, sembolik bağlar, arşiv ve kasa türü adlar
gelen kutusu sayılmaz.

Başka bir yer istiyorsan kurarken söyle:

```sh
python3 beyin.py yakala kur --klasor "Notlar/Yakala"
```

- Klasör vault'un içinde olmalı. Dışarı çıkan bir yol reddedilir ve hiçbir şey yazılmaz.
- Web Clipper şablonu, skill, oturum bildirimi ve `durum` aynı klasörü gösterir. Klasörü
  değiştirdiysen şablonu Web Clipper'a yeniden aktar.
- Eski klasördeki kartlar taşınmaz. `kur` kaç kartın geride kaldığını söyler; işlenmelerini
  istiyorsan onları yeni klasöre sen taşı.
- Seçim bu makinenin durum klasöründe saklanır. Aynı vault'u başka bir makinede de kullanıyorsan
  orada da aynı komutu çalıştır.
- Kayıtlı klasör silinir ya da adı değişirse yukarıdaki kurala dönülür; `durum` kullanılan klasörü söyler.

## İşleme nasıl çalışır

`python3 beyin.py yakala isle` bekleyen kartların metnini çıkarır, sonra ajan dersleri
`knowledge/` notlarına bağlar ve kartı `bitti` ile kapatır.

| Kaynak | Yol |
|---|---|
| Web Clipper ile gelen sayfa | Yakalanan metin kullanılır, ağa çıkılmaz |
| YouTube | [Defuddle](https://github.com/kepano/defuddle) ile bölümlü transkript; olmazsa `yt-dlp` altyazısı; o da yoksa yalnız ses indirilir, yerel Whisper ile yazıya dökülür, ses silinir |
| X, Reddit, GitHub, makale | Defuddle; yoksa düz HTTP ile metin |
| Mail | Seçili metin; tamamı için ajanın Gmail/Outlook bağlantısı |
| PDF, metin dosyası | `pdftotext` ya da doğrudan okuma; görselleri ajan açar |

Ham metin kart klasörünün içinde `.ham/` altında durur. Nokta ile başlayan klasör olduğu için
aramaya ve Obsidian'a karışmaz. `.ham/` ve yakalanan dosyaların durduğu `dosyalar/`, içlerine yazılan
`.gitignore` ile kart klasörü hangisi olursa olsun depoya da girmez.

### İsteğe bağlı araçlar

Hiçbiri zorunlu değil; varsa işleme genişler, yoksa elde olanla devam edilir.
`python3 beyin.py yakala durum` hangilerinin bulunduğunu söyler.

- **Node.js** (`npx`): Defuddle için. Sabit sürüm (`defuddle@0.19.4`) çalıştırılır.
- **yt-dlp**: video altyazısı ve ses.
- **Whisper**: Mac'te `mlx_whisper`, diğerlerinde `whisper` ya da `whisper-ctranslate2`. Ses indirmeyi istemezsen `isle --ses-yok`.
- **pdftotext** (Poppler): PDF metni.

## Gizlilik

- Yakalama ağa çıkmaz; yalnız vault'a bir dosya yazar.
- Ağ yalnız `isle` adımında ve yalnız yakaladığın kaynaklar için kullanılır. Defuddle,
  X gönderisi sayfada yoksa gönderi adresini FxTwitter API'sine sorar.
- Mail, pano metni ve dosya kartları `visibility: private` ile yazılır.
- Kaldırmak için `python3 beyin.py yakala kaldir`: kısayol, skill ve bildirim gider, kartların kalır.

## Komutlar

```text
beyin.py yakala              yakalama penceresi
beyin.py yakala ekle URL|DOSYA|METIN [--neden "..."]
beyin.py yakala liste [--durum bekliyor|cikarildi|islendi|hata]
beyin.py yakala isle [ID ...] [--ses-yok] [--tekrar]
beyin.py yakala bitti ID --bilgi knowledge/concepts/x.md [--ozet "..."]
beyin.py yakala kur [--kisayol-yok] [--tus 'ctrl+alt+b'] [--klasor "Notlar/Yakala"]
beyin.py yakala kisayol ['cmd+"']
beyin.py yakala kaldir | durum | sablon
```
