import { fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ShotCard } from "./api";
import {
  formatShotCardsForClipboard,
  missingMotionShotIds,
  promptTextFromShotTableClipboard,
  ShotCardEditor,
} from "./ShotCardEditor";
import { sampleFactsShot } from "./test/shotFacts";

function editableShot(): ShotCard {
  return {
    shot_id: "s1",
    start_time: 0,
    end_time: 4,
    shot_type: "中景",
    composition: "人物居中",
    camera_motion: "固定机位",
    subject: "讲解者",
    action: "人物从院子走向门口并抬手示意",
    scene: "院子",
    spoken_text: "",
    transition: "同镜头连续",
    motion: {
      subject_motion_state: "WALKING+GESTURING_ONLY",
      subject_direction: "lateral",
      subject_displacement: "从院子走到门口",
      hand_action: "抬手示意",
      camera_motion: "STATIC",
      relative_motion: "人物横向移动",
    },
  };
}

describe("分镜运动编辑", () => {
  it("核对路人只切换本地查看，不改变主要主体或已有动作", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, { shots: [sampleFactsShot()], onChange }),
    );
    fireEvent.change(screen.getByLabelText("s1 核对主体"), {
      target: { value: "p2" },
    });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByLabelText("s1 主要主体")).toHaveValue("p1");
    expect(screen.getByLabelText("s1 p2 位移")).toHaveValue("UNKNOWN");
    fireEvent.change(screen.getByLabelText("s1 p2 局部手势"), {
      target: { value: "GESTURING" },
    });
    const changed = onChange.mock.calls[0]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(changed.facts.primary_subject_id).toBe("p1");
    expect(
      changed.facts.actions.find((action) => action.subject_id === "p1"),
    ).toEqual(sampleFactsShot().facts.actions[0]);
    expect(
      changed.facts.actions.find((action) => action.subject_id === "p2")
        ?.gesture,
    ).toBe("GESTURING");
  });

  it("只读模式仍可切换核对主体，但所有事实保持不可编辑", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, {
        shots: [sampleFactsShot()],
        onChange,
        readOnly: true,
      }),
    );
    expect(screen.getByLabelText("s1 核对主体")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("s1 核对主体"), {
      target: { value: "p2" },
    });
    expect(screen.getByLabelText("s1 主要主体")).toBeDisabled();
    expect(screen.getByLabelText("s1 p2 位移")).toBeDisabled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("损坏的事实不回落静止，取消结构修正不丢原始数据", () => {
    const raw = { camera_shot_id: "camera-1", broken: "保留原始观察" };
    const shot = { ...sampleFactsShot(), facts: raw };
    const onChange = vi.fn();
    render(createElement(ShotCardEditor, { shots: [shot], onChange }));
    expect(screen.getByLabelText("s1 动作")).toHaveValue("结构化事实待确认");
    expect(
      screen.getByText("修正结构化记录").closest("details"),
    ).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("修正结构化记录"));
    fireEvent.change(screen.getByLabelText("s1 结构化记录修正"), {
      target: { value: "{invalid" },
    });
    fireEvent.click(screen.getByRole("button", { name: "取消修正" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(shot.facts).toEqual(raw);
    expect(formatShotCardsForClipboard([shot])).toContain(JSON.stringify(raw));
  });

  it("结构修正必须通过本地结构验证，采用时旧证据全部失效", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, {
        shots: [{ ...sampleFactsShot(), facts: { incomplete: true } }],
        onChange,
      }),
    );
    fireEvent.click(screen.getByText("修正结构化记录"));
    fireEvent.change(screen.getByLabelText("s1 结构化记录修正"), {
      target: { value: "{}" },
    });
    fireEvent.click(screen.getByRole("button", { name: "采用修正记录" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "结构化记录字段不完整或类型不正确",
    );
    fireEvent.change(screen.getByLabelText("s1 结构化记录修正"), {
      target: { value: JSON.stringify(sampleFactsShot().facts) },
    });
    fireEvent.click(screen.getByRole("button", { name: "采用修正记录" }));
    const changed = onChange.mock.calls[0]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(changed.facts.actions).toEqual(sampleFactsShot().facts.actions);
    expect(
      changed.facts.evidence.every((item) => item.status === "STALE"),
    ).toBe(true);
  });

  it("未知可见情况的人数保持待确认，不填成零", () => {
    const shot = sampleFactsShot();
    shot.facts.subjects[0].visibility = "UNKNOWN";
    render(createElement(ShotCardEditor, { shots: [shot], onChange: vi.fn() }));
    expect(screen.getByLabelText("s1 人数")).toHaveValue(null);
  });

  it("结构化事实驱动展示，旧动作、人数、运镜和转场不再独立编辑", () => {
    render(
      createElement(ShotCardEditor, {
        shots: [sampleFactsShot()],
        onChange: vi.fn(),
      }),
    );
    for (const label of [
      "动作",
      "主体",
      "人数",
      "运镜",
      "转场",
      "分段类型",
      "分段原因",
    ]) {
      expect(screen.getByLabelText(`s1 ${label}`)).toBeDisabled();
    }
    expect(screen.getByLabelText("s1 动作")).not.toHaveValue("旧人物静止描述");
    expect(
      (screen.getByLabelText("s1 动作") as HTMLInputElement).value,
    ).toContain("行走");
    expect(screen.getByLabelText("s1 人数")).toHaveValue(2);
    expect(screen.getByLabelText("s1 p1 位移")).toHaveValue("MOVING");
    expect(screen.getByLabelText("s1 p1 步态")).toHaveValue("WALKING");
    expect(screen.getByLabelText("s1 p1 局部手势")).toHaveValue("GESTURING");
    expect(screen.getByLabelText("s1 摄影机运动")).toHaveValue("FOLLOW");
  });

  it("切换主要主体不把原主体动作转移给未识别动作的新主体", () => {
    const shot = sampleFactsShot();
    const onChange = vi.fn();
    const view = render(
      createElement(ShotCardEditor, { shots: [shot], onChange }),
    );
    fireEvent.change(screen.getByLabelText("s1 主要主体"), {
      target: { value: "p2" },
    });
    const changed = onChange.mock.calls[0]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(changed.facts?.primary_subject_id).toBe("p2");
    expect(changed.facts?.actions).toEqual(shot.facts?.actions);
    view.rerender(
      createElement(ShotCardEditor, { shots: [changed], onChange }),
    );
    expect(screen.getByLabelText("s1 p2 位移")).toHaveValue("UNKNOWN");
    expect(screen.getByLabelText("s1 p2 步态")).toHaveValue("UNKNOWN");
    fireEvent.change(screen.getByLabelText("s1 p2 位移"), {
      target: { value: "MOVING" },
    });
    const edited = onChange.mock.calls[1]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(
      edited.facts?.actions.find((action) => action.subject_id === "p2"),
    ).toMatchObject({
      translation: "MOVING",
      gait: "UNKNOWN",
      turning: "UNKNOWN",
      gesture: "UNKNOWN",
      path: "",
      hand_detail: "",
    });
    expect(
      edited.facts?.actions.find((action) => action.subject_id === "p1"),
    ).toEqual(shot.facts?.actions[0]);
  });

  it("修改某个动作维度只使关联证据过期，保留未修改维度与机位证据", () => {
    const shot = sampleFactsShot();
    const onChange = vi.fn();
    render(createElement(ShotCardEditor, { shots: [shot], onChange }));
    fireEvent.change(screen.getByLabelText("s1 p1 步态"), {
      target: { value: "UNKNOWN" },
    });
    const changed = onChange.mock.calls[0]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(changed.facts?.actions[0]).toMatchObject({
      translation: "MOVING",
      gait: "UNKNOWN",
      gesture: "GESTURING",
    });
    expect(changed.facts?.evidence[0]).toEqual({
      ...shot.facts?.evidence[0],
      status: "STALE",
    });
    expect(changed.facts?.evidence[1]).toEqual(shot.facts?.evidence[1]);
    expect(shot.facts?.evidence[0].status).toBe("CURRENT");
  });

  it("修改时间保留小数并使原时间范围的全部证据过期", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, { shots: [sampleFactsShot()], onChange }),
    );
    fireEvent.change(screen.getByLabelText("s1 开始时间"), {
      target: { value: "0.375" },
    });
    const changed = onChange.mock.calls[0]?.[1] as ReturnType<
      typeof sampleFactsShot
    >;
    expect(changed.start_time).toBe(0.375);
    expect(
      changed.facts?.evidence.every((evidence) => evidence.status === "STALE"),
    ).toBe(true);
  });

  it("复制结构化事实原文及事实派生文字，仍保持旧TSV导入路径", () => {
    const shot = sampleFactsShot();
    const text = formatShotCardsForClipboard([shot]);
    expect(text).toContain("结构化事实(JSON)");
    expect(text).toContain(JSON.stringify(shot.facts));
    expect(text).not.toContain("旧人物静止描述");
    const imported = promptTextFromShotTableClipboard(text);
    expect(imported).toMatchObject({
      ok: true,
      promptText: expect.stringContaining(JSON.stringify(shot.facts)),
    });
  });

  it("组合运动同时显示行走与手势，不回落为静止，并保留原数据", () => {
    const shot = editableShot();
    render(createElement(ShotCardEditor, { shots: [shot], onChange: vi.fn() }));
    const select = screen.getByLabelText("s1 人物运动") as HTMLSelectElement;
    expect(
      Array.from(select.selectedOptions, (option) => option.value),
    ).toEqual(["WALKING", "GESTURING_ONLY"]);
    expect(shot.motion?.subject_motion_state).toBe("WALKING+GESTURING_ONLY");
  });

  it("未知人物、方向与镜头运动明确显示待确认", () => {
    const shot = editableShot();
    Object.assign(shot.motion ?? {}, {
      subject_motion_state: "UNKNOWN",
      subject_direction: "unknown",
      camera_motion: "UNKNOWN",
    });
    render(createElement(ShotCardEditor, { shots: [shot], onChange: vi.fn() }));
    for (const name of ["人物运动", "位移方向", "运镜方式"]) {
      const select = screen.getByLabelText(`s1 ${name}`) as HTMLSelectElement;
      expect(select.selectedOptions[0]?.textContent).toBe("待确认");
    }
  });

  it("无法识别的历史枚举保留原值并显示待确认", () => {
    const shot = editableShot();
    Object.assign(shot.motion ?? {}, {
      subject_motion_state: "FUTURE_STATE",
      camera_motion: "FUTURE_CAMERA",
    });
    render(createElement(ShotCardEditor, { shots: [shot], onChange: vi.fn() }));
    for (const name of ["人物运动", "运镜方式"]) {
      const select = screen.getByLabelText(`s1 ${name}`) as HTMLSelectElement;
      expect(select.selectedOptions[0]?.textContent).toContain("待确认");
    }
    expect(shot.motion?.subject_motion_state).toBe("FUTURE_STATE");
  });

  it.each([
    ["ORBIT", "环绕"],
    ["CRANE_UP", "升高"],
    ["CRANE_DOWN", "降低"],
    ["ZOOM_IN", "变焦拉近"],
    ["ZOOM_OUT", "变焦拉远"],
  ])("正确展示运镜 %s", (value, label) => {
    const shot = editableShot();
    Object.assign(shot.motion ?? {}, { camera_motion: value });
    render(createElement(ShotCardEditor, { shots: [shot], onChange: vi.fn() }));
    const select = screen.getByLabelText("s1 运镜方式") as HTMLSelectElement;
    expect(select.selectedOptions[0]?.textContent).toBe(label);
  });

  it("首次补标行走不会凭空写入无位移和固定机位", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, {
        shots: [{ ...editableShot(), motion: null }],
        onChange,
      }),
    );
    fireEvent.change(screen.getByLabelText("s1 人物运动"), {
      target: { value: "WALKING" },
    });
    expect(onChange.mock.calls[0]?.[1].motion).toEqual({
      subject_motion_state: "WALKING",
      subject_direction: "unknown",
      subject_displacement: "待确认",
      hand_action: "待确认",
      camera_motion: "UNKNOWN",
      relative_motion: "待确认",
    });
  });

  it("切换为静止时清除并列的行走和手势状态", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, { shots: [editableShot()], onChange }),
    );
    const select = screen.getByLabelText("s1 人物运动") as HTMLSelectElement;
    for (const option of Array.from(select.options)) {
      option.selected = ["STATIC", "WALKING", "GESTURING_ONLY"].includes(
        option.value,
      );
    }
    fireEvent.change(select);
    expect(onChange.mock.calls[0]?.[1].motion.subject_motion_state).toBe(
      "STATIC",
    );
  });

  it.each(["RUNNING", "MOVING"])(
    "改为 %s 时保留伴随手势，但不叠加另一个位移方式",
    (state) => {
      const onChange = vi.fn();
      render(
        createElement(ShotCardEditor, { shots: [editableShot()], onChange }),
      );
      const select = screen.getByLabelText("s1 人物运动") as HTMLSelectElement;
      for (const option of Array.from(select.options)) {
        option.selected = ["WALKING", state, "GESTURING_ONLY"].includes(
          option.value,
        );
      }
      fireEvent.change(select);
      expect(onChange.mock.calls[0]?.[1].motion.subject_motion_state).toBe(
        `${state}+GESTURING_ONLY`,
      );
    },
  );

  it("显示服务端定位到分镜的一致性问题", () => {
    render(
      createElement(ShotCardEditor, {
        shots: [editableShot()],
        onChange: vi.fn(),
        issues: [
          {
            code: "MOTION_CONFLICT",
            shot_id: "s1",
            message: "动作描述行走，但运动状态为静止。",
          },
        ],
      }),
    );
    expect(
      screen.getByRole("status", { name: "分镜待确认事项" }),
    ).toHaveTextContent("s1");
    expect(
      screen.getByRole("status", { name: "分镜待确认事项" }),
    ).toHaveTextContent("动作描述行走，但运动状态为静止。");
  });

  it("人数冲突可直接修正，未标注人数不会显示零", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, { shots: [editableShot()], onChange }),
    );
    const input = screen.getByLabelText("s1 人数");
    expect(input).toHaveValue(null);
    fireEvent.change(input, { target: { value: "1" } });
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        person_count: 1,
        motion: editableShot().motion,
      }),
    );
  });

  it("分段矛盾可修正类型和边界依据，不必改掉正确的转场描述", () => {
    const onChange = vi.fn();
    const shot = { ...editableShot(), segment_kind: "SHOT_CUT" as const };
    render(createElement(ShotCardEditor, { shots: [shot], onChange }));
    fireEvent.change(screen.getByLabelText("s1 分段类型"), {
      target: { value: "ACTION_BEAT" },
    });
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({
        segment_kind: "ACTION_BEAT",
        transition: "同镜头连续",
      }),
    );
    fireEvent.change(screen.getByLabelText("s1 分段原因"), {
      target: { value: "同一镜头内人物开始行走" },
    });
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ boundary_reason: "同一镜头内人物开始行走" }),
    );
  });
});

describe("missingMotionShotIds", () => {
  const withMotion: ShotCard = {
    shot_id: "s1",
    start_time: 0,
    end_time: 4,
    shot_type: "中景",
    composition: "人物居中",
    camera_motion: "固定",
    subject: "主讲人",
    action: "面向镜头讲解",
    scene: "室内",
    spoken_text: "",
    transition: "硬切",
    motion: {
      subject_motion_state: "STATIC",
      subject_direction: "in_place",
      subject_displacement: "无位移",
      hand_action: "双手自然下垂",
      camera_motion: "STATIC",
      relative_motion: "人物与摄影机相对静止",
    },
  };

  it("列出缺运动信息的镜头编号（旧版拆解）", () => {
    const legacy: ShotCard = {
      ...withMotion,
      shot_id: "s2",
      motion: undefined,
    };

    expect(missingMotionShotIds([withMotion, legacy])).toEqual(["s2"]);
  });

  it("全部镜头都有运动时返回空数组", () => {
    expect(missingMotionShotIds([withMotion])).toEqual([]);
  });

  it("镜头编号为空时按序号兜底命名", () => {
    const unnamed: ShotCard = {
      ...withMotion,
      shot_id: "  ",
      motion: undefined,
    };

    expect(missingMotionShotIds([unnamed])).toEqual(["第 1 镜"]);
  });

  it("混合顺序下匿名序号取原数组下标，不因先过滤而错位", () => {
    const named: ShotCard = { ...withMotion, shot_id: "s1" };
    const unnamedMissing: ShotCard = {
      ...withMotion,
      shot_id: "",
      motion: undefined,
    };

    expect(missingMotionShotIds([named, unnamedMissing])).toEqual(["第 2 镜"]);
  });
});

describe("formatShotCardsForClipboard", () => {
  it("复制全片风格信息，避免导入后丢失拍摄风格", () => {
    const text = formatShotCardsForClipboard([editableShot()], {
      theme: "庭院讲解",
      visual_style: "纪实",
      color_tone: "暖色",
      pace: "缓慢",
      camera_language: "单镜头连续跟拍",
      duration_seconds: 4,
    });
    for (const value of [
      "庭院讲解",
      "纪实",
      "暖色",
      "缓慢",
      "单镜头连续跟拍",
    ]) {
      expect(text).toContain(value);
    }
    expect(promptTextFromShotTableClipboard(text)).toEqual(
      expect.objectContaining({
        ok: true,
        promptText: expect.stringContaining("单镜头连续跟拍"),
      }),
    );
  });

  it("复制与导入保留动作段边界、场景、姿态和声音信息", () => {
    const shot = {
      ...editableShot(),
      person_count: 1,
      segment_kind: "ACTION_BEAT" as const,
      boundary_reason: "同镜头内人物开始移动",
      scene_dressing: "门口有一张石桌",
      scene_lighting: "左侧柔光",
      wardrobe_pose_detail: "蓝色上衣，重心前移",
      ambient_sound: "鸟鸣",
      music_style_hint: "轻音乐",
    };
    const text = formatShotCardsForClipboard([shot]);
    for (const value of [
      "ACTION_BEAT",
      "同镜头内人物开始移动",
      "门口有一张石桌",
      "左侧柔光",
      "蓝色上衣，重心前移",
      "鸟鸣",
      "轻音乐",
      "行走＋手势",
    ])
      expect(text).toContain(value);
    const imported = promptTextFromShotTableClipboard(text);
    expect(imported).toEqual(
      expect.objectContaining({
        ok: true,
        promptText: expect.stringContaining("ACTION_BEAT"),
      }),
    );
  });

  it("输出可直接粘贴到表格的完整制表符分镜", () => {
    const shot: ShotCard = {
      shot_id: "镜头 1",
      start_time: 0,
      end_time: 3.5,
      shot_type: "中景",
      composition: "人物居中",
      camera_motion: "推进",
      subject: "设计师",
      action: "走向镜头\n并抬手",
      scene: "乡村院落",
      spoken_text: "这栋房子的采光很好",
      transition: "硬切",
      motion: {
        subject_motion_state: "WALKING",
        subject_direction: "toward_camera",
        subject_displacement: "约 2 米",
        hand_action: "抬右手",
        camera_motion: "FOLLOW",
        relative_motion: "镜头跟随人物",
      },
    };

    const output = formatShotCardsForClipboard([shot]);

    expect(output.split("\n")).toHaveLength(2);
    expect(output).toContain("镜头编号\t开始(秒)\t结束(秒)");
    expect(output).toContain("镜头 1\t0\t3.5\t中景");
    expect(output).toContain("人物：行走；方向：向镜头");
    expect(output).toContain("走向镜头 并抬手");
  });

  it("只接受复制按钮产生的分镜表，并包装为待转 H3 的提示词", () => {
    const imported = promptTextFromShotTableClipboard(
      "镜头编号\t开始(秒)\t结束(秒)\t动作\ns1\t0\t4\t面向镜头",
    );
    expect(imported).toEqual(
      expect.objectContaining({
        ok: true,
        promptText: expect.stringContaining("H3"),
      }),
    );

    expect(promptTextFromShotTableClipboard("普通文案")).toEqual({
      ok: false,
      error: "未识别到分镜表表头，请先使用拆解页面的“复制分镜表”按钮。",
    });
    expect(
      promptTextFromShotTableClipboard("镜头编号\t开始(秒)\t结束(秒)"),
    ).toEqual({
      ok: false,
      error: "分镜表中没有可导入的镜头数据。",
    });
  });
});
