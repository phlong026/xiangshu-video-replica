type Intent<T> = {
  key: string;
  value: T;
  family: string;
  familyEpoch: number;
  isCurrent: () => boolean;
  completion: Promise<void>;
  resolve: () => void;
  reject: (cause: unknown) => void;
};

/**
 * 云端草稿写入队列（T35 扩展）。
 *
 * 除原有的「同 key 内容幂等 + 只保留最新 pending」外，增加 family 墓碑：
 * 放弃草稿时调用 {@link discard} 作废该账号（family）尚未开始的排队写入，
 * 并让已在途的写入完成后被静默吞掉——成功不记 lastSuccessKey，失败不上抛，
 * 由服务端墓碑（revision 围栏）负责拒绝真正的写回。这样「放弃 → 迟到的
 * 自动保存」不会再把旧稿写回云端，也不会向用户弹无关的保存失败提示。
 */
export function createCloudDraftQueue<T>(write: (value: T) => Promise<void>) {
  let active: Intent<T> | undefined;
  let pending: Intent<T> | undefined;
  let lastSuccessKey: string | undefined;
  const familyEpochs = new Map<string, number>();

  const epochOf = (family: string) => familyEpochs.get(family) ?? 0;

  const start = (intent: Intent<T>) => {
    if (!intent.isCurrent() || epochOf(intent.family) !== intent.familyEpoch) {
      intent.resolve();
      return;
    }
    active = intent;
    void write(intent.value).then(
      () => {
        if (epochOf(intent.family) === intent.familyEpoch)
          lastSuccessKey = intent.key;
        intent.resolve();
        if (active === intent) active = undefined;
        const next = pending;
        pending = undefined;
        if (next) start(next);
      },
      (cause) => {
        if (epochOf(intent.family) === intent.familyEpoch) intent.reject(cause);
        else intent.resolve();
        if (active === intent) active = undefined;
        const next = pending;
        pending = undefined;
        if (next) start(next);
      },
    );
  };

  const discard = (family: string) => {
    familyEpochs.set(family, epochOf(family) + 1);
    // lastSuccessKey 是内容寻址的幂等去重键；不清掉的话，放弃后原样重敲的
    // 内容会被误判为「已保存过」而跳过云端写入，草稿在下台设备就会消失。
    lastSuccessKey = undefined;
    if (pending?.family === family) {
      const superseded = pending;
      pending = undefined;
      superseded.resolve();
    }
  };

  const persist = (
    key: string,
    value: T,
    family: string,
    isCurrent: () => boolean,
  ) => {
    if (!active && !pending && lastSuccessKey === key) return Promise.resolve();
    if (active?.key === key) return active.completion;

    let resolveIntent: (() => void) | undefined;
    let rejectIntent: ((cause: unknown) => void) | undefined;
    const completion = new Promise<void>((resolve, reject) => {
      resolveIntent = resolve;
      rejectIntent = reject;
    });
    const intent: Intent<T> = {
      key,
      value,
      family,
      familyEpoch: epochOf(family),
      isCurrent,
      completion,
      resolve: () => resolveIntent?.(),
      reject: (cause) => rejectIntent?.(cause),
    };

    if (active) {
      // 尚未开始的写入没有服务端副作用，只保留最新完整草稿。
      const superseded = pending;
      if (superseded) {
        void completion.then(superseded.resolve, superseded.reject);
      }
      pending = intent;
    } else {
      start(intent);
    }
    return completion;
  };

  return { persist, discard };
}
