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

  it("改为跑动时保留伴随手势，但不产生行走加跑动的矛盾组合", () => {
    const onChange = vi.fn();
    render(
      createElement(ShotCardEditor, { shots: [editableShot()], onChange }),
    );
    const select = screen.getByLabelText("s1 人物运动") as HTMLSelectElement;
    for (const option of Array.from(select.options)) {
      option.selected = ["WALKING", "RUNNING", "GESTURING_ONLY"].includes(
        option.value,
      );
    }
    fireEvent.change(select);
    expect(onChange.mock.calls[0]?.[1].motion.subject_motion_state).toBe(
      "RUNNING+GESTURING_ONLY",
    );
  });

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
