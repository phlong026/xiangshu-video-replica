import { TabBar } from "./ui/TabBar";
import { useHashParam } from "./ui/useHashParam";
import { ViralCollectionRecords } from "./ViralCollectionRecords";
import { ViralKeywordsManager } from "./ViralKeywordsManager";
import { ViralPlatformProbes } from "./ViralPlatformProbes";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

const TABS = [
  { id: "plan", label: "计划与预算" },
  { id: "keywords", label: "关键词" },
  { id: "platforms", label: "平台与记录" },
] as const;

type CollectionTab = (typeof TABS)[number]["id"];

/**
 * 采集设置（整改 T37）：三类内容各占一个页签，采集记录不再与设置混排。
 * 页签写入地址（?sub=），带批次号进来（从内容概览点某次采集）时直接落在「平台与记录」。
 */
export function ViralCollectionPage({
  readOnly = false,
  initialBatchId = "",
}: {
  readOnly?: boolean;
  initialBatchId?: string;
}) {
  const [tab, setTab] = useHashParam<CollectionTab>(
    "sub",
    "plan",
    TABS.map(({ id }) => id),
    initialBatchId ? "platforms" : undefined,
  );
  return (
    <section className="admin-content-settings" aria-label="采集设置">
      <header className="admin-panel">
        <p className="admin-hint">
          维护采集计划、关键词、质量规则与预算；修改后由后台执行。
        </p>
      </header>
      <TabBar
        active={tab}
        ariaLabel="采集设置页签"
        items={TABS.map(({ id, label }) => ({ id, label }))}
        onChange={(next) => setTab(next as CollectionTab)}
      />
      {tab === "plan" ? <ViralRuntimeSection readOnly={readOnly} /> : null}
      {tab === "keywords" ? <ViralKeywordsManager readOnly={readOnly} /> : null}
      {tab === "platforms" ? (
        <>
          <ViralPlatformProbes readOnly={readOnly} />
          <ViralCollectionRecords initialBatchId={initialBatchId} />
        </>
      ) : null}
    </section>
  );
}
