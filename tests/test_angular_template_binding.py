from pathlib import Path
import networkx as nx

from graph_fy.detect import classify_file, FileType
from graph_fy.extract import _get_extractor
from graph_fy.extractors.angular import extract_angular_template, is_angular_template
from graph_fy.affected import affected_nodes


def test_angular_template_detection_and_classification(tmp_path: Path) -> None:
    html_file = tmp_path / "user-profile.component.html"
    html_file.write_text("<div>Hello</div>", encoding="utf-8")

    assert is_angular_template(html_file) is True
    assert classify_file(html_file) is FileType.CODE
    assert _get_extractor(html_file) == extract_angular_template

    regular_html = tmp_path / "index.html"
    regular_html.write_text("<html></html>", encoding="utf-8")
    assert is_angular_template(regular_html) is False


def test_angular_template_bindings_standalone(tmp_path: Path) -> None:
    html_content = """
<div class="user-card" *ngIf="isLoaded">
  <h2>{{ getFullName() }}</h2>
  <p>{{ bio }}</p>
  <ul>
    <li *ngFor="let pet of pets" (click)="onSelectPet(pet)">
      <span>{{ pet.name }}</span>
      <button [disabled]="isPending()" (click)="deletePet(pet.id)">Delete</button>
    </li>
  </ul>
  <input [(ngModel)]="searchQuery" [placeholder]="placeholderText" />
</div>
"""
    tmpl_path = tmp_path / "pet-list.component.html"
    tmpl_path.write_text(html_content, encoding="utf-8")

    result = extract_angular_template(tmpl_path)
    nodes = result["nodes"]
    edges = result["edges"]

    bound_targets = {e["target"] for e in edges if e.get("relation") == "template_binds_to"}

    # Verify event bindings
    assert any("onselectpet" in t for t in bound_targets)
    assert any("deletepet" in t for t in bound_targets)

    # Verify structural directives
    assert any("isloaded" in t for t in bound_targets)
    assert any("pets" in t for t in bound_targets)

    # Verify two-way binding
    assert any("searchquery" in t for t in bound_targets)

    # Verify property binding and signal invocation
    assert any("ispending" in t for t in bound_targets)
    assert any("placeholdertext" in t for t in bound_targets)


def test_angular_companion_ts_linking_and_blast_radius(tmp_path: Path) -> None:
    ts_content = """
import { Component } from '@angular/core';

@Component({
  selector: 'app-user',
  templateUrl: './user.component.html'
})
export class UserComponent {
  isBusy: boolean = false;
  onSave(): void {}
}
"""
    html_content = """
<div>
  <button [disabled]="isBusy" (click)="onSave()">Save</button>
</div>
"""
    ts_path = tmp_path / "user.component.ts"
    html_path = tmp_path / "user.component.html"

    ts_path.write_text(ts_content, encoding="utf-8")
    html_path.write_text(html_content, encoding="utf-8")

    result = extract_angular_template(html_path)
    edges = result["edges"]

    # Verify template_for edge to component class
    template_for = [e for e in edges if e.get("relation") == "template_for"]
    assert len(template_for) == 1
    assert "usercomponent" in template_for[0]["target"]

    # Build small DiGraph to verify reverse blast radius
    g = nx.DiGraph()
    for n in result["nodes"]:
        g.add_node(n["id"], **n)
    for e in edges:
        g.add_edge(e["source"], e["target"], **e)

    # Find the onSave target node
    save_nodes = [e["target"] for e in edges if "onsave" in e["target"]]
    assert len(save_nodes) >= 1
    save_nid = save_nodes[0]

    # Running affected_subgraph on onSave method should reach user.component.html via template_binds_to
    hits = affected_nodes(g, save_nid, depth=2)
    hit_ids = [h.node_id for h in hits]
    assert any("user_component_html" in hid or "user.component.html" in str(hid) for hid in hit_ids)
