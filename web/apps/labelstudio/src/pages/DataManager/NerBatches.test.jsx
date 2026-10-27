import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { NerBatches, NerBatchEntry } from "./NerBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: React.forwardRef(({ children, look, size, ...props }, ref) => React.createElement("button", { ...props, ref }, children)) };
});
const h = (c) => c.repeat(64);
const counts = { agreement: 0, disagreement: 1, unavailable: 0 };
const meta = { id: 7, project_id: 4, viewer: h("e"), name: "NER synthetic", schema: "ner-batches/v1",
  rule: "ner-strict-entities/v1", selection: "active-assignment-latest/v1", explanation: "ner-overlap-explanation/v1",
  engine: "nervaluate/0.2.0/strict-correct-counter", created_at: "created", calculated_at: "frozen", scope_enumerated_at: "preview",
  visible_task_count: 1, visibility: "project", manifest_hash: h("a") };
const capability = { ...meta, enabled: true, can_create: true };
const pins = [1,2].map((id) => ({ submission_id: id, assignment_id: id, assignee_id: id+10, task_id: 8, revision: 1,
  submitter_id: id+10, result_hash: h("b"), config_hash: h("c"), task_data_hash: h("d") }));
const row = { task_id: 8, outcome: "disagreement", original_outcome: "disagreement", reason: null, source_status: "verified",
  input_count: 2, total_pairs: 1, comparable_pairs: 1, calculation_hash: h("f") };
const entity = { start: 0, end: 5, label: "PER" };
const comparison = { availability: "available", reason: null, input_count: 2, total_pairs: 1, comparable_pairs: 1, unavailable_pairs: 0,
  pairs: [{ left: 1, right: 2, left_entities: 1, right_entities: 1, matching_entities: 0, f1: { numerator: "0", denominator: "1" },
    availability: "available", reason: null }],
  selected_pair: { left: 1, right: 2, text: "Alice 😀 李", control: { from_name:"ner", to_name:"text", field:"text", labels:["PER"] },
    left_entities:[entity], right_entities:[{ ...entity, end:4 }], matches:[],
    groups:[{ start:0, end:5, left:[0], right:[0], kind:"boundary_difference" }] } };
const respond = async (method, options) => {
  switch (method) {
    case "nerBatchCapability": return capability;
    case "nerBatches": return { project_id:4, viewer:h("e"), count:1, next:null, previous:null, results:[meta] };
    case "nerBatchPreview": return { schema:meta.schema, project_id:4, viewer:h("e"), preview_token:"signed", plan_hash:h("d"),
      task_ids:[8], display_counts:counts, scope_enumerated_at:"preview" };
    case "createNerBatch": return { ...meta, replayed:false, idempotency_key:options.headers["Idempotency-Key"], plan_hash:h("d") };
    case "nerBatchDetail": return { ...meta, display_counts:counts };
    case "nerBatchItems": return { ...meta, batch_id:7, count:1, next:null, previous:null, display_counts:counts, results:[row] };
    case "nerBatchItem": return { ...meta, observed_at:"now", item:{ ...row, inputs:pins, comparison } };
    default: throw new Error("Unexpected transport: "+method);
  }
};
const mount = (locale="en-US",entry=false) => {
  const runtime=createLocaleRuntime(locale), Provider=runtime.provider;
  return { ...render(<Provider>{entry ? <NerBatchEntry projectId={4} /> : <NerBatches projectId={4}/>}</Provider>), runtime };
};
const create = async (zh=false) => {
  fireEvent.click(await screen.findByRole("button",{name:zh?"新建比较批次":"New comparison batch"}));
  fireEvent.change(screen.getByLabelText(zh?"批次名称":"Batch name"),{target:{value:"NER synthetic"}});
  fireEvent.click(screen.getByRole("button",{name:zh?"预览比较":"Preview comparison"}));
  fireEvent.click(await screen.findByRole("button",{name:zh?"创建冻结批次":"Create frozen batch"}));
};
beforeEach(()=>mockCallApi.mockReset().mockImplementation(respond));

it.each(["en-US","zh-CN"])("reuses the complete frozen queue flow without classification/arbitration calls in %s",async(locale)=>{
  const {runtime,unmount}=mount(locale),zh=locale==="zh-CN";
  await create(zh);
  fireEvent.click(await screen.findByRole("button",{name:zh?"查看冻结对照":"View frozen comparison"}));
  expect((await screen.findByTestId("ner-left-text")).textContent).toContain("Alice");
  expect(screen.getByTestId("ner-matrix").textContent).toContain("0.00%");
  expect(screen.getByTestId("ner-detail").textContent).toContain(zh?"边界":"different boundaries");
  fireEvent.click(screen.getByRole("button",{name:zh?"返回队列":"Back to queue"}));
  await screen.findByLabelText(zh?"比较状态":"Comparison state");
  expect(screen.getByRole("combobox").value).toBe("disagreement");
  expect(mockCallApi.mock.calls.every(([m])=>m.startsWith("ner")||m==="createNerBatch")).toBe(true);
  expect(mockCallApi.mock.calls.filter(([m])=>m==="createNerBatch")).toHaveLength(1);
  unmount();runtime.destroy();
});

it("retains original body/key after unknown acknowledgement and never posts during refresh",async()=>{
  let attempts=0;
  mockCallApi.mockImplementation(async(m,o)=>{
    if(m==="createNerBatch"&&++attempts===1)throw new Error("lost response");
    const r=await respond(m,o);
    return m==="createNerBatch"?{...r,replayed:true}:r;
  });
  const {runtime,unmount}=mount();await create();
  await screen.findByRole("button",{name:"Recover original request"});
  fireEvent.click(screen.getByRole("button",{name:"Back to batches"}));
  await screen.findByRole("button",{name:"NER synthetic"});
  fireEvent.click(screen.getByTestId("ner-batch-refresh"));
  await screen.findByRole("button",{name:"NER synthetic"});
  expect(attempts).toBe(1);
  fireEvent.click(screen.getByRole("button",{name:"Recover original request"}));
  await screen.findByText(/Original batch #7 was recovered/);
  const calls=mockCallApi.mock.calls.filter(([m])=>m==="createNerBatch");
  expect(calls[0][1].body).toEqual(calls[1][1].body);
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  unmount();runtime.destroy();
});

it("does not confirm a matching-key acknowledgement for a different frozen plan",async()=>{
  mockCallApi.mockImplementation(async(m,o)=>{
    const r=await respond(m,o);return m==="createNerBatch"?{...r,plan_hash:h("0")}:r;
  });
  const {runtime,unmount}=mount();await create();
  await screen.findByRole("button",{name:"Recover original request"});
  expect(screen.queryByText(/was saved/)).toBeNull();
  expect(mockCallApi.mock.calls.filter(([m])=>m==="createNerBatch")).toHaveLength(1);
  unmount();runtime.destroy();
});

it("clears delayed answer evidence after blur and does not issue writes",async()=>{
  let release;
  mockCallApi.mockImplementation((m,o)=>m==="nerBatchItem"?new Promise(r=>{release=r;}):respond(m,o));
  const {runtime,unmount}=mount();
  fireEvent.click(await screen.findByRole("button",{name:"NER synthetic"}));
  fireEvent.click(await screen.findByRole("button",{name:"View frozen comparison"}));
  await waitFor(()=>expect(release).toBeDefined());
  fireEvent.blur(window);
  await act(async()=>release(await respond("nerBatchItem",{})));
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(screen.queryByTestId("ner-matrix")).toBeNull();
  expect(mockCallApi.mock.calls.some(([m])=>m==="createNerBatch")).toBe(false);
  unmount();runtime.destroy();
});

it.each(["viewer","reference","fraction"])("rejects mismatched %s evidence",async(kind)=>{
  mockCallApi.mockImplementation(async(m,o)=>{
    const r=await respond(m,o);
    if(m!=="nerBatchItem")return r;
    if(kind==="viewer")return {...r,viewer:h("0")};
    if(kind==="reference")return {...r,manifest_hash:h("0")};
    return {...r,item:{...r.item,comparison:{...comparison,pairs:[{...comparison.pairs[0],f1:{numerator:"1",denominator:"1"}}]}}};
  });
  const {runtime,unmount}=mount();
  fireEvent.click(await screen.findByRole("button",{name:"NER synthetic"}));
  fireEvent.click(await screen.findByRole("button",{name:"View frozen comparison"}));
  await screen.findByRole("alert");expect(screen.queryByTestId("ner-left-text")).toBeNull();
  unmount();runtime.destroy();
});

it("shows source unavailability without answers or replacing frozen inputs",async()=>{
  mockCallApi.mockImplementation(async(m,o)=>{
    const r=await respond(m,o);
    return m==="nerBatchItem"?{...r,item:{...row,outcome:"unavailable",reason:"source_unavailable",
      source_status:"unavailable",calculation_hash:null,comparable_pairs:0,inputs:[],
      comparison:{availability:"unavailable",reason:"source_unavailable",input_count:2,total_pairs:1,pairs:null,selected_pair:null}}}:r;
  });
  const {runtime,unmount}=mount();
  fireEvent.click(await screen.findByRole("button",{name:"NER synthetic"}));
  fireEvent.click(await screen.findByRole("button",{name:"View frozen comparison"}));
  await screen.findByTestId("ner-unavailable");
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(screen.queryByTestId("ner-matrix")).toBeNull();
  unmount();runtime.destroy();
});

it("removes a revoked entry on blur/focus and ignores a stale capability",async()=>{
  let denied=false;
  mockCallApi.mockImplementation((m,o)=>denied?Promise.resolve({error:"denied"}):respond(m,o));
  const {runtime,unmount}=mount("en-US",true);
  await screen.findByTestId("open-ner-batches");
  denied=true;fireEvent.blur(window);fireEvent.focus(window);
  await waitFor(()=>expect(screen.queryByTestId("open-ner-batches")).toBeNull());
  unmount();runtime.destroy();
});
