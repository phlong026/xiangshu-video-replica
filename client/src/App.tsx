import { useEffect, useState } from "react";

import { getHealth } from "./api";
import "./styles.css";

type Page = "login" | "projects";
type ServiceState = "checking" | "connected" | "disconnected";

export function App() {
  const [page, setPage] = useState<Page>("login");
  const [serviceState, setServiceState] = useState<ServiceState>("checking");

  useEffect(() => {
    if (page !== "projects") {
      return;
    }

    let isActive = true;
    setServiceState("checking");

    getHealth()
      .then(() => {
        if (isActive) {
          setServiceState("connected");
        }
      })
      .catch(() => {
        if (isActive) {
          setServiceState("disconnected");
        }
      });

    return () => {
      isActive = false;
    };
  }, [page]);

  if (page === "login") {
    return (
      <main className="centered-shell">
        <section className="login-card" aria-labelledby="app-title">
          <span className="eyebrow">INTERNAL PREVIEW</span>
          <h1 id="app-title">短视频复刻工作台</h1>
          <p>面向内部员工的 P0 工程骨架</p>
          <button type="button" onClick={() => setPage("projects")}>
            进入工作台
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header>
        <div>
          <span className="eyebrow">VIDEO REPLICA</span>
          <h1>项目</h1>
        </div>
        <ServiceBadge state={serviceState} />
      </header>
      <section className="empty-state">
        <h2>还没有项目</h2>
        <p>项目创建与视频复刻流程将在后续任务中逐项接入。</p>
        <button type="button" disabled>
          新建项目（即将开放）
        </button>
      </section>
    </main>
  );
}

function ServiceBadge({ state }: { state: ServiceState }) {
  const labels: Record<ServiceState, string> = {
    checking: "正在连接本地服务",
    connected: "本地服务已连接",
    disconnected: "本地服务未连接",
  };

  return (
    <span className={`service-badge service-badge--${state}`} role="status">
      {labels[state]}
    </span>
  );
}
