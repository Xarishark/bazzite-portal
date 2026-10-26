import { fetchNoCors } from "@decky/api";

export interface ArtworkTarget {appid: number; images: string[]}
// Steam's native custom artwork slots: portrait, hero, logo, landscape.
const slots: Record<string, number> = {"cover.png": 0, "hero.png": 1, "logo.png": 2, "landscape.png": 3};

export async function refreshArtwork(targets: ArtworkTarget[]) {
  const apps = SteamClient.Apps as typeof SteamClient.Apps & {
    SetCustomArtworkForApp(appid: number, base64: string, extension: string, type: number): Promise<void>;
  };
  const images = new Map<string, string>();
  for (const target of targets) {
    if (!Number.isInteger(target.appid) || target.appid <= 0 || target.appid > 0xffffffff) continue;
    for (const name of target.images) {
      if (!Object.prototype.hasOwnProperty.call(slots, name)) continue;
      if (!images.has(name)) {
        const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 10000);
        try {
          const response = await fetchNoCors("http://127.0.0.1:1337/plugins/Bazzite%20Portal/assets/steam/" + name, {signal: abort.signal});
          if (!response.ok) throw Error("Bundled Steam artwork is unavailable.");
          const blob = await response.blob();
          if (blob.size > 2 * 1024 * 1024 || blob.type !== "image/png") throw Error("Invalid bundled Steam artwork.");
          const encoded = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(Error("Could not read bundled Steam artwork."));
            reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
            reader.readAsDataURL(blob);
          });
          images.set(name, encoded);
        } finally {clearTimeout(timer);}
      }
      await apps.SetCustomArtworkForApp(target.appid, images.get(name)!, "png", slots[name]);
    }
  }
}
