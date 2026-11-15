import "./oral.css";

export function OralJourney({
  step,
  done,
}: {
  step: 1 | 2 | 3;
  /** 三步各自的就绪状态：完成的打 ✓ 收窄，当前步高亮，未到的灰置。 */
  done: [boolean, boolean, boolean];
}) {
  return (
    <ol className="oral-journey" aria-label="数字人口播制作流程">
      {["视频分身", "克隆声音", "文案成片"].map((label, index) => {
        const complete = done[index];
        const current = step === index + 1;
        return (
          <li
            key={label}
            aria-current={current ? "step" : undefined}
            className={
              complete ? "is-done" : current ? "is-current" : undefined
            }
          >
            <span>{complete ? "✓" : `0${index + 1}`}</span>
            <strong>{label}</strong>
            <small>
              {complete
                ? "已就绪"
                : current
                  ? [
                      "上传真人视频，创建分身",
                      "上传声音样本，试听确认",
                      "输入文案，用自己的声音讲述",
                    ][index]
                  : "待准备"}
            </small>
          </li>
        );
      })}
    </ol>
  );
}
