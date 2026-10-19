/// <reference types="cypress" />

type Fixture = {
  password: string;
  project_id: number;
  users: {
    manager: { id: number; email: string };
    annotator_a: { id: number; email: string };
  };
  tasks: {
    release: {
      id: number;
      assignment_id: number;
      annotation_id: number;
      revision_1_submission_id: number;
      revision_2_submission_id: number;
      revision_3_submission_id: number;
    };
  };
};

describe("approved submission release workspace", () => {
  let fixture: Fixture;

  before(() => {
    cy.readFile(".enterprise-e2e.json").then((data) => {
      fixture = data as Fixture;
    });
  });

  const dataPage = () => `/projects/${fixture.project_id}/data`;

  const openReleaseWorkspace = () => {
    cy.loginAs(fixture.users.manager.email, fixture.password, dataPage());
    cy.visit(dataPage());
    cy.location("pathname", { timeout: 30000 }).should("eq", dataPage());
    cy.get('[data-testid="open-release-workspace"]', { timeout: 30000 }).should("be.visible").click();
    cy.get('[data-testid="submission-release-workspace"]', { timeout: 30000 }).should("exist");
  };

  it("releases only the selected approved immutable revision", () => {
    openReleaseWorkspace();

    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_1_submission_id}"]`)
      .should("contain.text", "Revision 1")
      .and("contain.text", "Rejected")
      .click();
    cy.get('[data-testid="release-status"]').should("contain.text", "Rejected");
    cy.get('[data-testid="release-result-snapshot"]').should("contain.text", "Positive");
    cy.get('[data-testid="release-approved-submission"]').should("not.exist");

    cy.request({
      url: `/api/submissions/${fixture.tasks.release.revision_1_submission_id}/release/`,
      failOnStatusCode: false,
    }).then((response) => {
      expect(response.status).to.eq(400);
      expect(response.body.detail).to.contain("Only approved submissions");
    });

    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_3_submission_id}"]`)
      .should("contain.text", "Revision 3")
      .and("contain.text", "Pending")
      .click();
    cy.get('[data-testid="release-status"]').should("contain.text", "Pending");
    cy.get('[data-testid="release-approved-submission"]').should("not.exist");

    cy.request({
      url: `/api/submissions/${fixture.tasks.release.revision_3_submission_id}/release/`,
      failOnStatusCode: false,
    }).its("status").should("eq", 400);

    cy.request(`/api/submissions/${fixture.tasks.release.revision_2_submission_id}/`).then((approvedResponse) => {
      expect(approvedResponse.status).to.eq(200);
      expect(approvedResponse.body.revision).to.eq(2);
      expect(approvedResponse.body.status).to.eq("approved");
      expect(approvedResponse.body.result_snapshot.annotation.result).to.deep.equal([
        {
          from_name: "sentiment",
          to_name: "text",
          type: "choices",
          value: { choices: ["Negative"] },
        },
      ]);

      cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_2_submission_id}"]`)
        .should("contain.text", "Revision 2")
        .and("contain.text", "Approved")
        .click();

      cy.get('[data-testid="release-status"]').should("contain.text", "Approved");
      cy.get('[data-testid="release-result-hash"]').should("contain.text", approvedResponse.body.result_hash);
      cy.get('[data-testid="release-review-metadata"]')
        .should("contain.text", "Approved")
        .and("contain.text", "e2e-reviewer@example.com");

      cy.get('[data-testid="release-approved-submission"]').click();

      cy.get('[data-testid="release-success"]', { timeout: 30000 })
        .should("contain.text", "Released revision 2")
        .and("contain.text", approvedResponse.body.result_hash);

      cy.readFile(`${Cypress.config("downloadsFolder")}/submission-${fixture.tasks.release.revision_2_submission_id}-revision-2.json`)
        .then((download) => {
          expect(download.submission_id).to.eq(fixture.tasks.release.revision_2_submission_id);
          expect(download.revision).to.eq(2);
          expect(download.result_hash).to.eq(approvedResponse.body.result_hash);
          expect(download.result_snapshot).to.deep.equal(approvedResponse.body.result_snapshot);
        });

      cy.request(`/api/submissions/${fixture.tasks.release.revision_2_submission_id}/release/`).then(
        (releaseResponse) => {
          expect(releaseResponse.status).to.eq(200);
          expect(releaseResponse.body.submission_id).to.eq(fixture.tasks.release.revision_2_submission_id);
          expect(releaseResponse.body.revision).to.eq(2);
          expect(releaseResponse.body.result_hash).to.eq(approvedResponse.body.result_hash);
          expect(releaseResponse.body.result_snapshot).to.deep.equal(approvedResponse.body.result_snapshot);
        },
      );
    });

    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_1_submission_id}"]`)
      .should("contain.text", "Rejected");
    cy.get(`[data-testid="release-submission-${fixture.tasks.release.revision_3_submission_id}"]`)
      .should("contain.text", "Pending");
  });

  it("does not expose release controls to an annotator", () => {
    cy.loginAs(fixture.users.annotator_a.email, fixture.password, dataPage());
    cy.visit(dataPage());
    cy.get('[data-testid="open-release-workspace"]').should("not.exist");

    cy.request({
      url: `/api/submissions/${fixture.tasks.release.revision_2_submission_id}/release/`,
      failOnStatusCode: false,
    }).its("status").should("eq", 403);
  });
});

export {};
