// 镜序 Studio 设计方向预览 · 本地回传服务
// 等价实现 qiaomu-design-preview-server：静态托管 + 选择回传 + selection.json 落盘 + 哨兵日志
import { createServer } from "node:http";
import { readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const SELECTION_PATH = join(ROOT, "selection.json");
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");

  if (req.method === "POST" && url.pathname === "/api/select") {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", async () => {
      try {
        const payload = JSON.parse(body || "{}");
        if (!payload || typeof payload.id !== "string") {
          throw new Error("missing id");
        }
        await writeFile(
          SELECTION_PATH,
          `${JSON.stringify(payload, null, 2)}\n`,
          "utf8",
        );
        console.log(`QIAOMU_DESIGN_SELECTION::${JSON.stringify(payload)}`);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, selection: payload }));
      } catch (error) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: String(error) }));
      }
    });
    return;
  }

  if (req.method === "GET" && url.pathname === "/api/selection") {
    try {
      if (!existsSync(SELECTION_PATH)) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, selection: null }));
        return;
      }
      const raw = await readFile(SELECTION_PATH, "utf8");
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, selection: JSON.parse(raw) }));
    } catch {
      res.writeHead(500);
      res.end();
    }
    return;
  }

  const pathname = url.pathname === "/" ? "/index.html" : url.pathname;
  const filePath = normalize(join(ROOT, pathname));
  if (!filePath.startsWith(ROOT)) {
    res.writeHead(403);
    res.end("forbidden");
    return;
  }
  try {
    const content = await readFile(filePath);
    res.writeHead(200, {
      "Content-Type": MIME[extname(filePath)] ?? "application/octet-stream",
      "Cache-Control": "no-store",
    });
    res.end(content);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("not found");
  }
});

server.listen(0, "127.0.0.1", () => {
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  const previewUrl = `http://127.0.0.1:${port}/`;
  console.log(`QIAOMU_DESIGN_PREVIEW_URL::${previewUrl}`);
  console.log(`预览目录: ${ROOT}`);
  console.log("等待页面选择回传（POST /api/select → selection.json）…");
});
