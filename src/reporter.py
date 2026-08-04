import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

def generate_excel_report(audit_results, key_col, compare_cols):
    output = io.BytesIO()
    wb = Workbook()
    
    font_family = "Segoe UI"
    header_font = Font(name=font_family, size=11, bold=True, color="FFFFFF")
    title_font = Font(name=font_family, size=14, bold=True, color="1F2937")
    regular_font = Font(name=font_family, size=10)
    bold_font = Font(name=font_family, size=10, bold=True)
    
    # Colors
    header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid") # Indigo
    match_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")    # Green
    diff_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")     # Red
    warning_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")  # Yellow

    thin_border = Border(
        left=Side(style='thin', color='E5E7EB'),
        right=Side(style='thin', color='E5E7EB'),
        top=Side(style='thin', color='E5E7EB'),
        bottom=Side(style='thin', color='E5E7EB')
    )
    
    # 1. Resumen de Auditoría Sheet
    ws1 = wb.active
    ws1.title = "Informe de Auditoría"
    ws1.views.sheetView[0].showGridLines = True
    
    ws1.append(["Reporte de Conciliación OCR vs Excel"])
    ws1.cell(row=1, column=1).font = title_font
    ws1.append(["Columna Llave (Documento):", str(key_col)])
    ws1.cell(row=2, column=1).font = regular_font
    ws1.cell(row=2, column=2).font = bold_font
    ws1.append([]) # Spacer
    
    headers = ["Página PDF", "Documento Detectado", "Nombre Extraído OCR", "Estado Auditoría", "Detalle de Alertas"]
    for col in compare_cols:
        headers.extend([f"{col} (Excel)", f"{col} (Similitud %)"])
    
    ws1.append(headers)
    for col_idx, h in enumerate(headers, 1):
        cell = ws1.cell(row=4, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        
    for r_idx, row_data in enumerate(audit_results, 5):
        row_values = [
            row_data["Página"],
            row_data["Documento"],
            row_data["Nombre OCR"],
            row_data["Estado"],
            row_data["Detalle"]
        ]
        for col in compare_cols:
            row_values.extend([
                row_data.get(f"{col}_excel", "N/A"),
                row_data.get(f"{col}_score", "N/A")
            ])
            
        ws1.append(row_values)
        
        # Apply style and colors based on status
        status = row_data["Estado"]
        fill_color = match_fill if "Verificado" in status else (diff_fill if "Revisar" in status else warning_fill)
        
        for c_idx in range(1, len(row_values) + 1):
            cell = ws1.cell(row=r_idx, column=c_idx)
            cell.font = regular_font
            cell.border = thin_border
            # Color status columns
            if c_idx == 5:
                cell.fill = fill_color
                cell.font = bold_font

    # Adjust widths
    for col in ws1.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.row > 3 and cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws1.column_dimensions[col_letter].width = max(max_len + 4, 12)
        
    wb.save(output)
    return output.getvalue()
