/// <reference types="cypress" />

type Fixture = {
  password: string;
  project_id: number;
  users: { manager: { email: string } };
};

describe("Data Manager export locale", () => {
  let fixture: Fixture;

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => { fixture = data as Fixture; });
  });

  const chooseLocale = (locale: "en-US" | "zh-CN") => {
    cy.loginAs(fixture.users.manager.email, fixture.password, "/user/account/personal-info");
    cy.intercept({ method: "PATCH", pathname: /^\/api\/current-user\/locale\/?$/ }).as("exportLocaleSaved");
    cy.get('[data-testid="language-preference-select"]').then(($select) => {
      if ($select.val() !== locale) {
        cy.get('[data-testid="language-preference-select"]').should('be.enabled').select(locale);
        cy.wait("@exportLocaleSaved").then(({ response }) => {
          expect(response?.statusCode).to.eq(200);
          expect(response?.body.preference).to.eq(locale);
        });
      }
    });
    cy.get('[data-testid="language-preference-select"]').should("have.value", locale).and("not.be.disabled");
    cy.request("/api/current-user/locale/").its("body.preference").should("eq", locale);
    cy.get("html").should("have.attr", "lang", locale);
  };

  for (const [locale, copy] of [
    ["en-US", { heading: "Export data", format: "COCO with Images", description: "COCO format with the images included." }],
    ["zh-CN", { heading: "导出数据", format: "COCO（含图像）", description: "包含图像文件的 COCO 格式。" }],
  ] as const) {
    it(`keeps the converter format ID and downloaded bytes while displaying ${locale}`, () => {
      chooseLocale(locale);
      cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export/formats` }, [
        { name: "JSON", title: "JSON", description: "Raw JSON", tags: [] },
        { name: "COCO_WITH_IMAGES", title: "COCO with Images", description: "COCO format with images downloaded.", tags: ["image segmentation"] },
      ]).as("formats");
      cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export` }, (request) => {
        expect(request.query.exportType).to.eq("COCO_WITH_IMAGES");
        request.reply({
          statusCode: 200,
          headers: { "content-type": "application/octet-stream", filename: "synthetic-export.json" },
          body: "synthetic-export-bytes",
        });
      }).as("download");

      cy.visit(`/projects/${fixture.project_id}/data/export`);
      // The parent data workspace can finish initialization after the modal
      // first renders. Act on the settled workspace, not a replaced button.
      cy.window().its("dataManager").should("exist");
      cy.wait("@formats");
      cy.get(`[aria-label="${copy.heading}"]`).should("be.visible");
      cy.get('[data-testid="export-format-COCO_WITH_IMAGES"]')
        .should("contain.text", copy.format)
        .and("contain.text", copy.description)
        .focus().type("{enter}")
        .should("have.attr", "aria-pressed", "true");

      let downloadedName = "";
      let downloadedBlob: Blob | undefined;
      cy.window().then((win: any) => {
        cy.stub(win.URL, "createObjectURL").callsFake((blob: Blob) => {
          downloadedBlob = blob;
          return "blob:synthetic-export";
        });
        cy.stub(win.HTMLAnchorElement.prototype, "click").callsFake(function (this: HTMLAnchorElement) {
          downloadedName = this.download;
        }).as("downloadClick");
      });
      cy.get(`[aria-label="${copy.heading}"]`).click();
      cy.wait("@download");
      // The intercepted response completes before the page reads its blob and clicks the link.
      cy.get("@downloadClick").should("have.been.calledOnce");
      cy.then(async () => {
        expect(downloadedName).to.eq("synthetic-export.json");
        expect(await downloadedBlob?.text()).to.eq("synthetic-export-bytes");
      });
    });
  }

  for (const [locale, copy] of [
    ["en-US", { heading: "Import Data", cancel: "Cancel import", finish: "Finish import" }],
    ["zh-CN", { heading: "导入数据", cancel: "取消导入", finish: "完成导入" }],
  ] as const) {
    it(`opens the Data Manager import modal in ${locale} without writing project data`, () => {
      chooseLocale(locale);
      const projectWrites: string[] = [];
      let previewRequests = 0;
      cy.intercept({ method: /POST|PUT|PATCH|DELETE/, url: `**/api/projects/${fixture.project_id}/**` }, (request) => {
        const pathname = new URL(request.url).pathname;
        // The existing sample-task POST computes a preview and does not persist a task.
        if (pathname.endsWith("/sample-task")) previewRequests += 1;
        else projectWrites.push(`${request.method} ${pathname}`);
      });
      cy.intercept({ method: "POST", pathname: `/api/projects/${fixture.project_id}/sample-task` }).as("samplePreview");
      cy.visit(`/projects/${fixture.project_id}/data/import`);
      cy.get("body").should("contain.text", copy.heading);
      cy.get(`[aria-label="${copy.cancel}"]`).should("be.visible");
      cy.get(`[aria-label="${copy.finish}"]`).should("exist");
      // The modal shell can render before the project and its preview are ready.
      cy.wait("@samplePreview").its("response.statusCode").should("eq", 200);
      cy.then(() => {
        expect(previewRequests, "sample preview loaded").to.be.greaterThan(0);
        expect(projectWrites, "opening import must not persist project data").to.deep.equal([]);
      });
    });
  }

  it("shows Chinese timeout guidance without changing the CLI command", () => {
    chooseLocale("zh-CN");
    cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export/formats` }, [
      { name: "JSON", title: "JSON", description: "Raw JSON", tags: [] },
    ]);
    cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export` }, {
      statusCode: 504,
      body: "synthetic timeout",
    }).as("exportTimeout");
    cy.visit(`/projects/${fixture.project_id}/data/export`);
    cy.get('[aria-label="导出数据"]').click();
    cy.wait("@exportTimeout");
    cy.get("body").should("contain.text", "导出超时");
    cy.get("code").should("have.text", `label-studio export ${fixture.project_id} JSON --export-path=<output-path>`);
  });

  it("does not crash when the format list is unavailable", () => {
    chooseLocale("zh-CN");
    cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export/formats` }, {
      statusCode: 503,
      body: { detail: "synthetic unavailable" },
    }).as("formatsUnavailable");
    cy.visit(`/projects/${fixture.project_id}/data/export`);
    cy.wait("@formatsUnavailable");
    cy.get('[aria-label="导出数据"]').should("be.disabled");
    cy.get('[role="status"]').should("contain.text", "无法读取导出格式");
    cy.get('[data-testid^="export-format-"]').should("not.exist");
  });

  for (const [locale, title, guidance] of [
    ["en-US", "Export data", "Export approved submissions through Release"],
    ["zh-CN", "导出数据", "请通过“发布”导出已获批提交"],
  ] as const) {
    it(`explains immutable export protection in ${locale} without requesting raw export`, () => {
      chooseLocale(locale);
      let rawRequests = 0;
      cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export` }, () => { rawRequests++; });
      cy.intercept({ method: "GET", pathname: `/api/projects/${fixture.project_id}/export/formats` }, {
        statusCode: 400,
        body: { status_code: 400, detail: "Validation error", display_context: { reason: "IMMUTABLE_RELEASE_REQUIRED" } },
      }).as("immutableFormats");
      cy.visit(`/projects/${fixture.project_id}/data/export`);
      cy.wait("@immutableFormats");
      cy.get('[role="status"]').should("contain.text", guidance);
      cy.get(`[aria-label="${title}"]`).should("be.disabled");
      cy.get('[data-testid^="export-format-"]').should("not.exist");
      cy.then(() => { expect(rawRequests).to.eq(0); });
    });
  }
});
