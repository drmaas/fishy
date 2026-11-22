# WI ingest fixtures

Tiny catalog HTML, one-lake 24K Hydro GeoJSON, and minimal PDFs for `pipeline` unit tests. CI never hits the network.

Ada Lake on the live DNR catalog uses map key `0417300a` and WBIC `0417300`.

`catalog-pager-encoded.html` is a pager fragment with HTML-entity apostrophes around the ASP.NET Next postback target. Altoona Lake uses a SWIMS `DownloadDocument` URL and map key `2128100a`.