/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * 视频生成批次的 Provider 路由："fake_h3" 走本地模拟（不产生付费调用），
   * 未设置或 "metaso" 走真实 MiniMax H3。默认 metaso。
   */
  readonly VITE_GENERATION_PROVIDER?: "fake_h3" | "metaso";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
