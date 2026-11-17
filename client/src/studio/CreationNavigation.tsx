import { useStudio } from "./context";
import type { StudioPage } from "./types";
import { Tabs } from "./ui";

export function CreationNavigation() {
  const { state, navigate } = useStudio();
  const page =
    state.page === "reference"
      ? "video"
      : state.page === "replacement"
        ? "replica"
        : state.page;
  return (
    <div className="studio-creation-nav">
      <Tabs<StudioPage>
        value={page}
        onChange={navigate}
        items={[
          // 侧栏一级入口是「视频拆解」；<30s 与 >30s 在页签组内并列，
          // 长视频不占侧栏一级位置（2026-11-16 产品口径：分界线 15s → 30s）。
          { id: "replica", label: "<30s 视频拆解" },
          { id: "long-replica", label: "长视频拆解" },
          { id: "video", label: "AI 视频" },
          { id: "oral", label: "数字人口播" },
        ]}
      />
    </div>
  );
}
