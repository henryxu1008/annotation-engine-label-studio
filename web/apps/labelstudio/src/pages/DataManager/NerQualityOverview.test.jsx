import { act, fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { NerQualityOverview } from "./NerQualityOverview";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const f = (n, d=1) => ({ numerator: String(n), denominator: String(d) });
const h = (s) => s.repeat(64);
const reasons = [{reason:"missing_submission",count:1},{reason:"no_entities",count:1}];
const batch = {id:6,viewer:h("c"),visibility:"project",manifest_hash:h("a"),calculated_at:"batch-time",visible_task_count:4};
const person = (actor_id) => ({actor_id,tasks:4,comparable_tasks:2,unavailable_tasks:2,peer_pairs:5,comparable_peer_pairs:3,
  unavailable_peer_pairs:2,unavailable_reasons:reasons,unavailable_peer_pair_reasons:reasons,
  task_coverage:f(1,2),pair_coverage:f(3,5),strict_f1:f(5,8),task_ids:[8,9,10,11]});
const response = () => ({schema:"ner-quality-overview/v1",rules:{aggregation:"ner-batch-quality-task-macro/v1",strict:"ner-strict-entities/v1",
  explanation:"ner-overlap-explanation/v1",engine:"nervaluate/0.2.0/strict-correct-counter",selection:"active-assignment-latest/v1"},
  scope:{kind:"batch",organization_id:1,project_id:4,batch_id:6,visibility:"project",reference_kind:"manifest_hash",reference_hash:h("a")},
  source_set_hash:h("b"),viewer:h("c"),scope_counts:{tasks:4,comparable_tasks:2,unavailable_tasks:2,unavailable_reasons:reasons,
    total_pairs:6,comparable_pairs:4,unavailable_pairs:2,unavailable_pair_reasons:reasons},
  source_status:"verified",current_source_issues:{task_count:0,reasons:[]},availability:"available",reason:null,
  summary:{task_coverage:f(1,2),pair_coverage:f(2,3),strict_f1:f(7,12),agreement_tasks:1,disagreement_tasks:1},
  annotators:[person(11),person(12)],pagination:{count:2,page:1,page_size:50,next_page:null,previous_page:null},
  frozen:"ner-overview-receipt",result_hash:h("d"),receipt_verified:true,batch_calculated_at:"batch-time",summarized_at:"first-time",observed_at:"now"});
const mount = (locale="en-US",navigation={current:{}}) => {
  const runtime=createLocaleRuntime(locale),Provider=runtime.provider,onOpenTask=jest.fn();
  const props={projectId:4,batch,navigation,onOpenTask};
  return {...render(<Provider><NerQualityOverview {...props}/></Provider>),runtime,Provider,props};
};
beforeEach(()=>mockCallApi.mockReset().mockResolvedValue(response()));

it.each(["en-US","zh-CN"])("shows distinct NER task and pair denominators, peer weights and readonly replay in %s",async(locale)=>{
  const v=mount(locale);fireEvent.click(screen.getByTestId("ner-quality-read"));
  expect((await screen.findByTestId("ner-quality-summary")).textContent).toContain("58.33%");
  expect(screen.getByTestId("ner-quality-summary").textContent).toContain("66.67%");
  expect(v.container.querySelector('[data-quality-actor="11"]').textContent).toContain("62.50%");
  expect(v.container.textContent).toContain(locale==="zh-CN"?"双方均无实体":"Both sides have no entities");
  fireEvent.click(v.container.querySelector('[data-quality-task="8"]'));
  expect(v.props.onOpenTask).toHaveBeenCalledWith(8);
  fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByTestId("ner-quality-summary");
  expect(mockCallApi.mock.calls[1][1].params.frozen).toBe("ner-overview-receipt");
  expect(mockCallApi.mock.calls.every(([m])=>m==="nerQualityOverview")).toBe(true);
  v.unmount();v.runtime.destroy();
});

it("masks the whole overview on source failure, retains the old receipt and recovers exactly",async()=>{
  const v=mount();fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByTestId("ner-quality-summary");
  mockCallApi.mockResolvedValue({...response(),source_status:"unavailable",availability:"unavailable",reason:"source_unavailable",
    summary:null,annotators:null,pagination:null,result_hash:null,receipt_verified:false,
    current_source_issues:{task_count:1,reasons:[{reason:"source_unavailable",count:1}]}});
  fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByText(/denominator is unchanged/);
  expect(screen.queryByTestId("ner-quality-summary")).toBeNull();expect(v.container.querySelector('[data-quality-task]')).toBeNull();
  expect(v.props.navigation.current.frozen).toBe("ner-overview-receipt");
  mockCallApi.mockResolvedValue(response());fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByTestId("ner-quality-summary");
  expect(mockCallApi.mock.calls[2][1].params.frozen).toBe("ner-overview-receipt");v.unmount();v.runtime.destroy();
});

it.each(["viewer","pairs","coverage","person","schema","source","receipt"])("rejects mismatched %s without displaying evidence",async(kind)=>{
  const data=response();
  if(kind==="viewer")data.viewer=h("e");
  if(kind==="pairs")data.scope_counts.total_pairs=8;
  if(kind==="coverage")data.summary.pair_coverage=f(1);
  if(kind==="person")data.annotators[0].comparable_peer_pairs=6;
  if(kind==="schema")data.schema="classification-quality-overview/v1";
  if(kind==="source")data.source_status="unavailable";
  if(kind==="receipt")data.receipt_verified=false;
  mockCallApi.mockResolvedValue(data);
  const v=mount();fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByRole("alert");
  expect(screen.queryByTestId("ner-quality-summary")).toBeNull();expect(v.container.querySelector('[data-quality-task]')).toBeNull();
  v.unmount();v.runtime.destroy();
});

it("keeps all-empty scores null and a zero pair denominator undefined",async()=>{
  const data=response();
  data.scope_counts={tasks:4,comparable_tasks:0,unavailable_tasks:4,unavailable_reasons:[{reason:"insufficient_inputs",count:4}],
    total_pairs:0,comparable_pairs:0,unavailable_pairs:0,unavailable_pair_reasons:[]};
  data.summary={task_coverage:f(0),pair_coverage:null,strict_f1:null,agreement_tasks:0,disagreement_tasks:0};
  data.availability="unavailable";data.reason="no_comparable_tasks";data.annotators=[];data.pagination.count=0;
  mockCallApi.mockResolvedValue(data);const v=mount();fireEvent.click(screen.getByTestId("ner-quality-read"));
  await screen.findByTestId("ner-quality-summary");expect(v.container.textContent).not.toContain("100.00%");
  expect(v.container.textContent).not.toContain("NaN");v.unmount();v.runtime.destroy();
});

it("discards delayed answers after blur or viewer change",async()=>{
  let release;mockCallApi.mockImplementation(()=>new Promise(r=>{release=r;}));
  const v=mount();fireEvent.click(screen.getByTestId("ner-quality-read"));fireEvent.blur(window);
  await act(async()=>release(response()));expect(screen.queryByTestId("ner-quality-summary")).toBeNull();
  mockCallApi.mockResolvedValue(response());
  v.rerender(<v.Provider><NerQualityOverview {...v.props} batch={{...batch,viewer:h("e")}}/></v.Provider>);
  fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByRole("alert");
  expect(screen.queryByTestId("ner-quality-summary")).toBeNull();v.unmount();v.runtime.destroy();
});

it.each([403,404,409,422,503])("clears prior scores on HTTP %s and never issues a write",async(status)=>{
  const v=mount();fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByTestId("ner-quality-summary");
  mockCallApi.mockResolvedValue({$meta:{ok:false,status}});fireEvent.click(screen.getByTestId("ner-quality-read"));await screen.findByRole("alert");
  expect(screen.queryByTestId("ner-quality-summary")).toBeNull();
  expect(mockCallApi.mock.calls.every(([m])=>m==="nerQualityOverview")).toBe(true);v.unmount();v.runtime.destroy();
});
